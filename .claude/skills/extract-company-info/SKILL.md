---
name: extract-company-info
description: This skill should be used when the user asks to "extract company info", "회사 정보 추출", "기업 정보", "company profile", or provides Wanted company page URLs (wanted.co.kr/company/*)
---

# 회사 정보 추출 스킬 (멀티소스)

<!-- shared-contract:start -->
## Shared Contract: packaged writer, storage, and privacy

- Fetch supported source candidates only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform {remember|saramin|thevc|wanted} --id {id}`.
- Persist candidates only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company apply --company-name {company_name} --input {candidate.md}`.
- Validate the resulting private file only with `UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {company_slug}.md`.
- Store company information only under `private/company_info/`.
- Never add, commit, push, publish, or quote private company information files or their contents.
<!-- shared-contract:end -->

여러 채용 플랫폼에서 기업 정보를 추출합니다. **HTTP/CLI 우선, 브라우저 fallback** 순서로 진행합니다.

## 추출 경로 우선순위

1. **CLI `company fetch`** (1차) — `UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform {remember|saramin|thevc|wanted} --id {id}` 로 구조화 데이터 즉시 추출. 브라우저 불필요.
2. **HTTP `__NEXT_DATA__`** (1차 대안) — Wanted 회사 페이지(`/company/{id}`)를 WebFetch/curl로 GET하면 `<script id="__NEXT_DATA__">` 안에 `companySummary`(평균연봉, 인원, 입사/퇴사, 매출)와 `companyInfo`(설립, 업종, 태그, 위치)가 JSON으로 포함.
3. **Chrome MCP 브라우저** (2차) — 경력별 제보 연봉(Wanted 모달, API 401), TheVC 투자 세부(로그인 필요) 등 HTTP로 접근 불가한 데이터만 브라우저로 보완.

> **교훈**: HTTP/CLI 경로가 있는데 브라우저 워크플로우를 관성적으로 따르면 ~15회 tool call 소모. CLI를 먼저 쓰면 ~1~2회로 기본 데이터 추출 가능.

## CLI 서브커맨드

```bash
# Remember (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id}

# Remember (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id} --json

# Saramin (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn}

# Saramin (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn} --json

# TheVC (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform thevc --id {company_id}

# TheVC (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform thevc --id {company_id} --json

# Wanted (마크다운 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform wanted --id {company_id}

# Wanted (JSON 출력)
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform wanted --id {company_id} --json

# 검증 (필수 — Phase 8)
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {slug}.md --fix
```

## 사전 요구사항

1. HTTP/CLI 추출: 추가 요구사항 없음
2. 브라우저 fallback: Chrome MCP 확장 설치, 플랫폼 로그인 (필요시)

## 지원 플랫폼

| 플랫폼 | CLI/HTTP 경로 | 브라우저 필요 데이터 |
|--------|----------|-----------|
| Wanted | CLI `company fetch --platform wanted` | 경력별 제보 연봉 (모달) |
| Remember | CLI `company fetch --platform remember` | 없음 |
| Saramin | CLI `company fetch --platform saramin` | 복지 상세, 면접후기 |
| TheVC | 기본 대시보드 (비로그인) | 투자 세부 금액, 투자사 (로그인/프로 플랜) |

## 추출 데이터 포인트

### 기본 정보
- 기업 소개 (회사 설명 텍스트)
- 기업 정보 (업종, 설립연도, 직원수, 위치)
- 연도별 매출 그래프 (연매출 추이)
- 월별 인원 통계 (총 인원, 입사자, 퇴사자)

### 연봉 정보 (우선순위: Remember > Wanted > Saramin)
- 월평균 급여
- 평균 연봉 (신입/경력별)
- 신입 예상 연봉 (학력별: 고졸/초대졸/대졸/대학원)
- 올해 입사자 평균 연봉
- **경력별 제보 연봉** (Wanted 모달에서 탭별 추출)

### 투자 정보 (TheVC - 스타트업 전용)
- 현재 라운드 (Seed/Pre-A/Series A/B/C/...)
- 누적 투자금
- 투자자 (주요 투자사 목록)
- 투자 이력 (라운드별 일자, 금액, 투자사)

### 복지/혜택 (Saramin > Wanted)
- 복지 태그
- 복지 상세 (지원금/보험, 근무 환경 등)

## 추출 워크플로우

### Phase 1: 입력 분석

```
입력 유형 판별:
1. URL 제공 → 플랫폼/ID 파싱 후 해당 경로 직접 접근
2. 회사명만 제공 → 자동 검색 모드 (멀티소스)
```

### Phase 2: 기존 파일 확인

```
private/company_info/ 에서 동일 회사 파일 존재 여부 확인.
있으면 덮어쓸지 보강할지 판단.
```

### Phase 3: 플랫폼별 데이터 추출

#### Wanted 추출

**Step 1: HTTP `__NEXT_DATA__` (기본 데이터)**

```
WebFetch 또는 curl로 wanted.co.kr/company/{id} GET
→ HTML에서 <script id="__NEXT_DATA__"> 추출
→ JSON 파싱 후 dehydrateState.queries 배열에서:
  [0] queryKey=["companyInfo", "{id}"]  → 설립, 업종, 태그, 위치, 설명
  [1] queryKey=["companySummary", "{hash}"] → salary, employee, sales
  [2] queryKey=["companyHire", "{id}"]  → 채용 포지션 목록
```

이 단계로 평균연봉, 인원(입사/퇴사), 매출, 태그 등 기본 데이터가 모두 추출된다.

**Step 2: 브라우저 (경력별 제보 연봉 — HTTP로 불가능한 데이터)**

```
1. navigate → wanted.co.kr/company/{id}
2. "경력 예상연봉" 섹션의 "자세히 보기" 버튼 클릭
3. 모달 창에서 각 탭(2-4년, 5-7년, 8-10년, 10년 초과) 순차 클릭
4. 각 탭의 "제보 내역" 테이블에서 기준연도, 제보연봉 추출
```

> 참고: 제보 내역이 없는 경력 구간은 "제보 내역이 없습니다" 메시지 표시
> 참고: Step 2는 priority-1 연봉 데이터(스크리닝 룰 5장)이므로, 스크리닝 대상 회사는 가능하면 수행

#### Remember 추출

**Step 1: CLI (권장)**

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform remember --id {company_id}
```

JSON 출력에서 name, address, industry, established, employee_count, avg_salary_manwon, salary_yoy_change, employee_stats(12개월), company_type, homepage, ceo, tags 추출.

> 주의: avg_salary_manwon은 이미 만원 단위로 변환됨 (원 단위 ÷ 10000).
> employee_stats는 정확한 입사/퇴사 수치 — 그래프 추정 불필요.

**Step 1 대안: HTTP `__NEXT_DATA__` (직접 파싱)**

```
career.rememberapp.co.kr/job/company/{id} GET → __NEXT_DATA__ JSON
dehydratedState.queries[1].state.data.data 에서:
  name, description, representativeName, type, establishmentDate
  industry: {id, level1, level2, level3}
  tags: string[]
  salaryStatistics: {average(원), changesFromLastYear(원), relatedCompaniesAverage}
  employeeStatistics: [{month, total, join, leave}, ...] (12개월, 최신순)

주의: average는 원 단위 (49799008 → 4,980만원). ÷10000 필요.
```

Remember는 경력별 제보 연봉 데이터를 제공하지 않음.

#### Saramin 추출

**Step 1: CLI (권장)**

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company fetch --platform saramin --id {csn}
```

마크다운 출력으로 기본 회사 정보 + 평균연봉 추출.

> csn(사업자등록번호)은 Saramin 회사 상세 페이지 URL에서 추출: `/company-info/view?csn={csn}`

**Step 2: 브라우저 (CLI로 불가능한 데이터)**

```
1. navigate → saramin.co.kr/zf_user/company-info/view?csn={csn}
2. scroll × 3회 → get_page_text
3. 파싱: 복지 상세, 면접 후기, 재무 정보 (CLI에 미포함)
```

> 중요: 검색 결과 페이지(/search/company)가 아닌 상세 페이지(/company-info/view)까지 이동해야 전체 정보 추출 가능

#### TheVC 추출 (스타트업)

```
1. navigate → thevc.kr/{company_slug}
2. computer(screenshot)으로 기본 정보 확인
3. "투자 유치" 탭 클릭:
   javascript_tool(action: "javascript_exec",
     text: "document.evaluate(\"//a[contains(text(),'투자 유치')]\",
       document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null)
       .singleNodeValue.click()")
4. get_page_text로 투자 이력 추출
```

### Phase 4: 멀티소스 검색 (회사명만 제공된 경우)

```
검색 URL 패턴:
- Wanted: wanted.co.kr/search?query={name}&tab=company
- Remember: career.rememberapp.co.kr/search?keyword={name}
- Saramin: saramin.co.kr/zf_user/search/company?searchword={name}
- TheVC: thevc.kr/integrated-search/overview?keyword={name}
  (주의: /search?keyword= 아님)
```

### Phase 5: 데이터 병합 및 정규화

```
1. 각 소스 데이터 수집
2. 필드별 우선순위 적용:
   - 연봉: Remember > Wanted > Saramin
   - 투자: TheVC only
   - 인원: Wanted > Saramin
   - 복지: Saramin > Wanted
3. 중복 제거 및 병합
4. 마크다운 포맷 생성
```

### Phase 6: 출력 포맷

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

### 신입 예상연봉

| 학력 | 예상 연봉 |
|------|----------|
| 고졸 | ± {N}만원 |
| 초대졸 | ± {N}만원 |
| 대졸 | ± {N}만원 |
| 대학원졸 | ± {N}만원 |

### 경력 예상연봉

| 경력 | 예상 연봉 |
|------|----------|
| 2-4년 | ± {N}만원 |
| 5-7년 | ± {N}만원 |
| 8-10년 | ± {N}만원 |
| 10년 초과 | ± {N}만원 |

### 제보 연봉 ({YYYY}년)

| 경력 | 제보 연봉 |
|------|----------|
| {N-M}년 | {N}만원 |
| {N-M}년 | {N}만원 |

> 참고: Wanted "경력 예상연봉" > "자세히 보기" > 각 탭별 "제보 내역"에서 추출

## 인원 통계

| 항목 | 수치 |
|------|------|
| 현재 인원 | {N}명 |
| 1년간 입사자 | {N}명 |
| 1년간 퇴사자 | {N}명 |
| 평균 근속연수 | {N}년 |

## 매출 추이

| 연도 | 매출 |
|------|------|
| 2023 | XX억원 |
| 2022 | XX억원 |

## 투자 정보 (스타트업)

| 항목 | 내용 |
|------|------|
| 현재 라운드 | {Series X} |
| 누적 투자금 | {N}억원 |
| 총 투자자 수 | {N}개사 |

### 투자 라운드 상세

| 라운드 | 일자 | 금액 | 투자자 |
|--------|------|------|--------|
| Series C | YYYY-MM-DD | {N}억원 | {투자사} |
| Series B | YYYY-MM-DD | {N}억원 | {투자사} |

## 복지/혜택

### 태그
- {태그1}
- {태그2}

### 복지 상세
**지원금/보험:**
- {항목}

**근무 환경:**
- {항목}

## 회사 소개

{회사 소개 텍스트}

---

*추출일: {YYYY-MM-DD}*
*출처:*
- {URL1}
- {URL2}
```

### Phase 7: 파일 저장

추출된 정보를 `private/company_info/` 디렉토리에 저장:
- 파일명: `{company_slug}.md` (소문자, 영문, 하이픈)
- 예: `acmelabs.md`, `acmestore.md`, `acmecorp.md`

## 운영 노하우 (축적 교훈)

### 동음이의어 감지

Wanted `search_company_id`는 검색 실패 시 첫 결과를 무비판 채택. 잘못된 회사 매칭 사례 다수 발생 (2026-04-26 감사에서 12건 적발).

**방지 절차:**
1. 추출 후 파일의 `# 제목`과 의도한 회사명 비교
2. 불일치 시 Wanted ID가 비정상적으로 낮은지 확인 (id 1~6 등은 fallback 강력 추정)
3. `UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {slug}.md --fix` 로 검증

### 보류 판정 시 멀티소스 심화 조사

스크리닝 결과가 **지원 보류**일 때만 추가 소스에서 교차 확인:
- 추천/비추천 확정 건은 추가 조사 불필요
- 최초 추출에 사용하지 않은 플랫폼(Wanted/Remember/TheVC/DART)에서 보완
- 상장사는 DART 재무 데이터도 포함
- 추가 소스가 없으면 시도한 사실을 company_info 파일에 기록

### JD ID 충돌 주의 (Wanted/Remember)

같은 5~6자리 숫자 ID가 Wanted와 Remember에서 다른 회사를 가리킬 수 있음.
자동 재조회 전 반드시 source URL 확인.

### company_info 중복 파일

auto-pipeline이 한글/영문/stub 변형을 생성하는 경우가 있음.
가장 큰 파일을 유지하고 나머지 삭제.

### TheVC 검색 URL

`https://thevc.kr/integrated-search/overview?keyword={keyword}` (NOT `/search?keyword=`)

### 투자 금액 검증

company validator는 `숫자+억` 패턴을 요구. "미공개"는 투자 필드를 충족하지 않음.

### 스타트업 판정

- Wanted 태그: 설립3년이하, 설립4~9년 → 스타트업
- 직원수 ≤ 300 + 비상장 → 스타트업 후보
- `현재 상태`가 `상장`/`M&A` → `is_startup=false` 유지 (TheVC 키워드 무시)

## 에러 처리

### 검색 결과 없음

```
"{회사명}"에 대한 검색 결과가 없습니다.
- 정확한 회사명 확인
- 영문/한글 변환 시도
- 직접 URL 제공 요청
```

### 플랫폼 접근 제한

```
{플랫폼}에서 일시적 접근 제한이 발생했습니다.
다른 소스에서 추출을 계속합니다.
```

### 회사 페이지 없음

```
"해당 회사의 {플랫폼} 페이지를 찾을 수 없습니다.
회사명으로 검색하거나 직접 URL을 제공해주세요."
```

## URL 패턴

| 플랫폼 | 유형 | URL 패턴 | 예시 |
|--------|------|----------|------|
| Wanted | 회사 프로필 | wanted.co.kr/company/{id} | /company/12345 |
| Wanted | 회사 검색 | wanted.co.kr/search?query={name}&tab=company | /search?query=예시랩스 |
| Remember | 회사 프로필 | career.rememberapp.co.kr/job/company/{id} | /job/company/2344515 |
| Remember | 회사 검색 | career.rememberapp.co.kr/search?keyword={name} | /search?keyword=예시회사 |
| Saramin | 회사 프로필 | saramin.co.kr/zf_user/company-info/view?csn={id} | /company-info/view?csn=xxx |
| Saramin | 회사 검색 | saramin.co.kr/zf_user/search/company?searchword={name} | /search/company?searchword=예시랩스 |
| TheVC | 회사 프로필 | thevc.kr/{company_slug} | /acmecorp |
| TheVC | 회사 검색 | thevc.kr/integrated-search/overview?keyword={name} | /integrated-search/overview?keyword=예시회사 |

## 플랫폼별 특이사항

### Wanted
- 연봉 순위 (상위 N%) 제공
- 채용 중인 포지션 목록
- **경력별 제보 연봉**: "경력 예상연봉" > "자세히 보기" 클릭 시 모달에서 탭별(2-4년/5-7년/8-10년/10년초과) 제보 내역 확인 가능

### Remember
- 작년 대비 연봉 변화율
- 월별 입사/퇴사 정확 수치 (12개월)

### Saramin
- **상세 페이지 필수**: `/zf_user/company-info/view?csn={csn}` 페이지에서만 전체 정보 추출 가능
- 국민연금/고용보험 기반 데이터

### TheVC
- 투자 라운드 상세 이력
- **스타트업/벤처 기업만 등록됨**
- B2G 계약, 국가 R&D, 특허 수

---

## Phase 8: 데이터 검증 및 리스크 플래깅 (필수)

파일 저장 후 **반드시** 검증 스크립트 실행:

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file {company_slug}.md --fix
```

- 이 명령은 사람이 읽는 결정적 요약과 종료 상태를 제공하며, 버전이 고정된
  JSON 계약은 노출하지 않는다.
- `현재 상태`가 `상장`/`M&A`인 경우 키워드(TheVC, Series 등)가 있어도 `is_startup=false`로 유지됨

### 검증 항목

**필수 필드 체크:**
- 모든 기업: 회사명, 설립연도, 직원수, 평균연봉
- 스타트업 추가: 투자 라운드, 누적 투자금, 1년간 입사자, 1년간 퇴사자

**자동 리스크 감지:**
| 코드 | 조건 | 스크리닝 영향 |
|------|------|--------------|
| TURNOVER_CRITICAL | 퇴사율 ≥ 50% | 즉시 주의 |
| TURNOVER_HIGH | 퇴사율 ≥ 30% | 조직 안정성 검토 |
| SHRINKING_FAST | 순감소 > 20% | 구조조정 가능성 |
| SALARY_LOW | 상위 50% 미만 | 연봉 협상 주의 |
| NO_INVESTMENT_DATA | 스타트업 투자정보 없음 | 추가 검증 필요 |

### 검증 결과 처리

1. **완성도 < 70%**: 누락 필드 보완 시도 (다른 소스 재검색)
2. **리스크 플래그 발견**: `--fix` 옵션으로 리스크 섹션 자동 추가
3. **검증 통과**: 완성도 N% 보고

### 스키마 참조

상세 필드 정의: `private/company_info/_schema.md`
