---
name: extract-company-info
description: This skill should be used when the user asks to "extract company info", "회사 정보 추출", "기업 정보", "company profile", or provides Wanted company page URLs (wanted.co.kr/company/*)
---

# 회사 정보 추출 스킬 (Codex — HTTP/CLI 전용)

<!-- shared-contract:start -->
## Shared Contract: packaged writer, storage, and privacy

- Fetch supported source candidates only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform {remember|saramin|wanted} --id {id}`.
- Persist candidates only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company apply --company-name {company_name} --input {candidate.md}`.
- Validate the resulting private file only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {company_slug}.md`.
- Store company information only under `private/company_info/`.
- Never add, commit, push, publish, or quote private company information files or their contents.
<!-- shared-contract:end -->

Codex 환경에는 브라우저 도구가 없다. CLI와 HTTP 요청만으로 추출한다.

## 사용 가능한 도구

| 도구 | 용도 |
|------|------|
| `career-jobs company fetch` | Remember, Saramin, Wanted 구조화 추출 |
| `career-jobs company validate` | 저장 후 검증 (필수) |

브라우저 전용 데이터(Wanted 경력별 제보 연봉, TheVC 투자 세부)는 이 환경에서 추출 불가.
해당 데이터가 필요하면 사용자에게 Claude Code 환경에서 추출하도록 안내한다.

## CLI 레퍼런스

```bash
# Remember (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id}

# Remember (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id} --json

# Saramin (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn}

# Saramin (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn} --json

# Wanted (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform wanted --id {company_id}

# Wanted (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform wanted --id {company_id} --json

# 검증 (필수)
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {slug}.md --fix
```

## 추출 워크플로우

### Phase 1: 입력 분석

```
1. URL → 플랫폼과 ID 파싱
   - wanted.co.kr/company/{id} → Wanted, id
   - career.rememberapp.co.kr/job/company/{id} → Remember, id
   - saramin.co.kr/zf_user/company-info/view?csn={csn} → Saramin, csn
   - thevc.kr/{slug} → TheVC (제한적 — 로그인 필요 데이터 추출 불가)
2. 회사명만 → 검색 모드 (아래 Phase 2)
```

### Phase 2: 회사 ID 확인 (회사명만 제공된 경우)

검색 URL을 curl로 GET하여 회사 ID를 파악한다.

```bash
# Wanted — __NEXT_DATA__ 내 검색 결과에서 company id 추출
curl -s 'https://www.wanted.co.kr/search?query={name}&tab=company' | \
  grep -o '__NEXT_DATA__.*</script>' | head -1

# Remember — HTML에서 /job/company/{id} 패턴 추출
curl -s 'https://career.rememberapp.co.kr/search?keyword={name}' | \
  grep -oP '/job/company/\d+' | head -5

# Saramin — 검색 결과에서 csn 추출
curl -s 'https://m.saramin.co.kr/job-search/company-search?searchword={name}' | \
  grep -oP 'csn=\d+' | head -5

# TheVC (주의: /search?keyword= 아님)
# https://thevc.kr/integrated-search/overview?keyword={name}
```

### Phase 3: 플랫폼별 데이터 추출

#### Wanted

```bash
# HTML GET → __NEXT_DATA__ JSON 파싱
curl -s 'https://www.wanted.co.kr/company/{id}' > /tmp/wanted_company.html

# __NEXT_DATA__ 추출 (Python one-liner)
python3 -c "
import json, re, sys
html = open('/tmp/wanted_company.html').read()
m = re.search(r'<script id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>', html)
data = json.loads(m.group(1))
queries = data['props']['pageProps']['dehydratedState']['queries']
for q in queries:
    key = str(q.get('queryKey', []))
    if 'companyInfo' in key:
        print('=== companyInfo ===')
        print(json.dumps(q['state']['data']['data'], ensure_ascii=False, indent=2))
    elif 'companySummary' in key:
        print('=== companySummary ===')
        print(json.dumps(q['state']['data']['data'], ensure_ascii=False, indent=2))
"
```

`companySummary` 구조:
- `salary.avg` — 평균연봉 (만원)
- `employee.total` — 현재 인원
- `employee.join` — 1년간 입사자
- `employee.leave` — 1년간 퇴사자
- `sales` — 매출 추이 배열

`companyInfo` 구조:
- `name`, `industryName`, `foundedYear`
- `address`, `homepageUrl`
- `tags` — 기업 태그 배열

#### Remember

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id}
```

JSON 필드:
- `name`, `address`, `industry`, `established`
- `employee_count`, `avg_salary_manwon`, `salary_yoy_change`
- `employee_stats` — 12개월 [{month, total, join, leave}, ...]
- `company_type`, `homepage`, `ceo`, `tags`

> `avg_salary_manwon`은 이미 만원 단위 변환 완료.

#### Saramin

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn}
```

마크다운 출력 (기업 정보 + 평균연봉).
`--json` 플래그로 구조화 데이터도 가능.

> csn은 사업자등록번호. Saramin 회사 상세 URL에서 추출.

#### TheVC

Codex에서는 기본 페이지만 curl로 접근 가능. 투자 세부 금액은 로그인 필요.

```bash
curl -s 'https://thevc.kr/{company_slug}' | \
  grep -oP '(Series [A-Z]|Seed|Pre-A|누적 투자|억원)' | head -10
```

제한적 추출만 가능. 상세 데이터가 필요하면 사용자에게 안내.

### Phase 4: 데이터 병합 → 마크다운 작성

필드별 우선순위:
- 연봉: Remember > Wanted > Saramin
- 투자: TheVC only
- 인원: Remember(정확 수치) > Wanted > Saramin
- 복지: Saramin > Wanted

출력 파일: `private/company_info/{company_slug}.md`

### Phase 5: 검증 (필수)

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {company_slug}.md --fix
```

## 출력 포맷

```markdown
# {회사명} ({영문명})

## 기업 정보

| 항목 | 내용 |
|------|------|
| 회사명 | (주){회사명} |
| 업종 | {업종} |
| 설립 | {YYYY년} ({N}년차) |
| 직원수 | {N}명 |
| 위치 | {주소} |
| 홈페이지 | {URL} |

## 연봉 정보

| 항목 | 금액 | 출처 |
|------|------|------|
| 평균 연봉 | **{N}만원** | {출처} |
| 작년 대비 | {+/-N}만원 ({+/-N}%) | {출처} |

## 인원 통계

| 항목 | 수치 |
|------|------|
| 현재 인원 | {N}명 |
| 1년간 입사자 | {N}명 |
| 1년간 퇴사자 | {N}명 |

## 매출 추이

| 연도 | 매출 |
|------|------|
| 2023 | XX억원 |

## 투자 정보 (스타트업)

| 항목 | 내용 |
|------|------|
| 현재 라운드 | {Series X} |
| 누적 투자금 | {N}억원 |

## 회사 소개

{회사 소개 텍스트}

---

*추출일: {YYYY-MM-DD}*
*출처:*
- {URL1}
- {URL2}
```

## 운영 노하우

### 동음이의어 감지

Wanted 검색 실패 시 첫 결과를 무비판 채택하는 버그 존재.
추출 후 `# 제목`과 의도한 회사명을 비교. Wanted ID가 1~6 등 비정상적으로 낮으면 fallback 강력 추정.

### JD ID 충돌 (Wanted/Remember)

같은 숫자 ID가 두 플랫폼에서 다른 회사를 가리킬 수 있음. source URL 반드시 확인.

### TheVC 검색 URL

`https://thevc.kr/integrated-search/overview?keyword={keyword}` (NOT `/search?keyword=`)

### company_info 중복

auto-pipeline이 한글/영문/stub 변형을 생성하는 경우 있음. 가장 큰 파일 유지, 나머지 삭제.

### 투자 금액 표기

validator는 `숫자+억` 패턴 요구. "미공개"는 투자 필드를 충족하지 않음.

### 스타트업 판정

- Wanted 태그(설립3년이하, 설립4~9년), 직원 ≤300 + 비상장 → 스타트업 후보
- `상장`/`M&A` 상태 → `is_startup=false` 유지

### 보류 판정 시 멀티소스

스크리닝 **지원 보류** 건만 추가 소스 심화 조사. 추천/비추천 확정 건은 불필요.

## 브라우저 전용 데이터 (Codex에서 추출 불가)

| 데이터 | 플랫폼 | 이유 |
|--------|--------|------|
| 경력별 제보 연봉 | Wanted | 모달 UI, API 401 |
| TheVC 투자 세부 | TheVC | 로그인 필요 |
| 복지 상세/면접후기 | Saramin | 브라우저 스크롤 |

이 데이터가 필요하면: "Claude Code에서 `/extract-company-info`로 브라우저 추출 필요" 안내.
