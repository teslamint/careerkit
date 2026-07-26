---
name: extract-company-info
description: This skill should be used when the user asks to "extract company info", "회사 정보 추출", "기업 정보", "company profile", or provides Wanted company page URLs (wanted.co.kr/company/*)
---

# 회사 정보 추출 스킬 (멀티소스)

여러 채용 플랫폼에서 기업 정보를 추출합니다. **HTTP 우선, 브라우저 fallback** 순서로 진행합니다.

## 추출 경로 우선순위

1. **HTTP `__NEXT_DATA__`** (1차) — Wanted 회사 페이지(`/company/{id}`)를 HTTP GET으로 요청하면 `<script id="__NEXT_DATA__">` 안에 `companySummary` 쿼리(평균연봉, 인원, 입사/퇴사, 매출)와 `companyInfo` 쿼리(설립, 업종, 태그, 위치)가 JSON으로 포함됨. 브라우저 없이 기본 데이터 전부 추출 가능.
2. **Chrome MCP 브라우저** (2차) — 경력별 제보 연봉(Wanted 모달, API 401 Unauthorized), TheVC 투자 세부 금액(로그인 필요) 등 HTTP로 접근 불가한 데이터만 브라우저로 보완.

> **근거**: 어느 스타트업 추출에서 메모리에 HTTP 경로가 있었으나 스킬의 브라우저 워크플로우를 관성적으로 따라 ~15회 tool call 소모. HTTP 경로를 먼저 썼으면 ~2회로 기본 데이터 추출 가능했음.

## 사전 요구사항

1. HTTP 추출: 추가 요구사항 없음
2. 브라우저 fallback: Chrome MCP 확장 설치, 플랫폼 로그인 (필요시)

## 지원 플랫폼

| 플랫폼 | HTTP 경로 | 브라우저 필요 데이터 |
|--------|----------|-----------|
| Wanted | `__NEXT_DATA__` JSON (연봉, 인원, 매출, 태그) | 경력별 제보 연봉 (모달) |
| Remember | `__NEXT_DATA__` JSON (연봉, 인원 입사/퇴사 정확 수치, 설립, 업종, 태그) | 경력별 제보 연봉 없음 |
| Saramin | - | 연봉, 복지, 면접후기 (전체) |
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
1. URL 제공 → 해당 플랫폼 직접 접근
2. 회사명만 제공 → 자동 검색 모드 (멀티소스)
```

### Phase 2: Chrome MCP 연결

```
mcp__claude-in-chrome__tabs_context_mcp 호출
- createIfEmpty: true
```

### Phase 3: 멀티소스 검색 (자동 검색 모드)

회사명만 제공된 경우:

```
1. tabs_create_mcp로 새 탭 생성
2. 각 플랫폼 순차 검색:
   a. navigate로 검색 페이지 이동
   b. computer(screenshot)으로 결과 확인
   c. 회사 프로필 페이지 식별 및 클릭
   d. 상세 페이지에서 데이터 추출
```

### Phase 4: 플랫폼별 데이터 추출

#### Wanted 추출

**Step 1: HTTP `__NEXT_DATA__` (기본 데이터)**

```python
# wanted.co.kr/company/{id} GET → __NEXT_DATA__ JSON 파싱
# dehydrateState.queries 배열에서:
#   [0] queryKey=["companyInfo", "{id}"]  → 설립, 업종, 태그, 위치, 설명
#   [1] queryKey=["companySummary", "{hash}"] → salary, employee, sales
#   [2] queryKey=["companyHire", "{id}"]  → 채용 포지션 목록
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

**Step 1: HTTP `__NEXT_DATA__` (기본 데이터 — 브라우저 불필요)**

```python
# career.rememberapp.co.kr/job/company/{id} GET → __NEXT_DATA__ JSON 파싱
# dehydratedState.queries[1].state.data.data 에서:
#   name, description, representativeName, type, establishmentDate
#   industry: {id, level1, level2, level3}
#   tags: string[]
#   salaryStatistics: {average(원), changesFromLastYear(원), relatedCompaniesAverage}
#   employeeStatistics: [{month, total, join, leave}, ...] (12개월, 최신순)
#
# 주의: average는 원 단위 (49799008 → 4,980만원). ÷10000 필요.
# employeeStatistics는 정확한 입사/퇴사 수치 — 그래프 추정 불필요.
```

이 단계로 연봉, 월별 인원(입사/퇴사 정확 수치), 설립일, 업종, 태그 등 기본 데이터가 모두 추출된다.
Remember는 경력별 제보 연봉 데이터를 제공하지 않음.

**Step 2: 브라우저 (fallback — HTTP로 안 되는 경우만)**

```
1. navigate → career.rememberapp.co.kr/job/company/{id}
2. computer(scroll, down, 5) × 3회
3. get_page_text로 텍스트 추출
4. 파싱: 채용포지션 목록
```

#### Saramin 추출

```
1. 검색 페이지로 이동:
   navigate → saramin.co.kr/zf_user/search/company?searchword={회사명}
2. computer(screenshot)으로 검색 결과 확인
3. 검색 결과에서 회사 찾기:
   - 회사명과 일치하는 결과 클릭
   - 또는 "기업정보" 링크 클릭
4. 상세 페이지 URL 확인:
   - URL이 /zf_user/company-info/view?csn={csn} 형태인지 확인
   - csn 값 추출하여 저장
5. 상세 페이지에서 데이터 추출:
   a. computer(scroll, down, 5) × 3회 (전체 페이지 로드)
   b. get_page_text로 텍스트 추출
   c. 파싱 대상:
      - 기업 기본정보 (대표자, 설립연도, 직원수, 업종)
      - 연봉 정보 (평균연봉, 신입/경력별 예상연봉)
      - 복지 정보 (4대보험, 퇴직금, 연차, 기타복지)
      - 면접 후기 (난이도, 질문 유형)
      - 기업 태그 (근무환경, 조직문화)
      - 재무 정보 (매출액, 영업이익 - 공시기업)
6. 출처 URL 저장:
   - 검색 URL이 아닌 상세 페이지 URL 저장
   - 형식: https://www.saramin.co.kr/zf_user/company-info/view?csn={csn}
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

## 에러 처리

### 검색 결과 없음

```
"{회사명}"에 대한 검색 결과가 없습니다.
- 정확한 회사명 확인
- 영문/한글 변환 시도
- 직접 URL 제공 요청
```

### 페이지 로드 불완전

```
스크롤 추가 수행 후 재추출
- 3회 스크롤 후에도 데이터 누락 시 사용자에게 알림
```

### 데이터 비공개

```
일부 정보가 비공개입니다:
- 연봉: [비공개] (업계 평균 {N}만원)
- 매출: 정보 없음
```

### 플랫폼 접근 제한

```
{플랫폼}에서 일시적 접근 제한이 발생했습니다.
다른 소스에서 추출을 계속합니다.
```

### 탭 컨텍스트 손실

```
tabs_context_mcp 재호출 후 createIfEmpty: true로 새 탭 생성
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
| Saramin | 회사 검색 | saramin.co.kr/zf_user/search?searchword={name} | /search?searchword=예시랩스 |
| TheVC | 회사 프로필 | thevc.kr/{company_slug} | /acmecorp |
| TheVC | 회사 검색 | thevc.kr/search?query={name} | /search?query=예시회사 |

## 사용 예시

### 예시 1: 회사명만 제공

```
사용자: "예시회사 회사 정보 알려줘"

1. Wanted 검색 → 회사 페이지 발견 → 데이터 추출
2. Remember 검색 → 회사 페이지 발견 → 데이터 추출
3. TheVC 검색 → 스타트업 확인 → 투자 정보 추출
4. 데이터 병합 → private/company_info/acmecorp.md 저장
```

### 예시 2: URL 직접 제공

```
사용자: "https://www.wanted.co.kr/company/12345 회사 정보 추출해줘"

1. Wanted 회사 페이지 직접 접근 → 데이터 추출
2. 회사명으로 TheVC 검색 → 투자 정보 추가 (스타트업인 경우)
3. private/company_info/{company_slug}.md 저장
```

### 예시 3: 멀티 URL 제공

```
사용자: "아래 URL들에서 기업 정보 추출해줘:
- https://www.wanted.co.kr/company/67890
- https://career.rememberapp.co.kr/job/company/2344515
- https://thevc.kr/acmecorp"

1. 각 URL 순차 접근
2. 각 소스에서 데이터 추출
3. 데이터 병합 → private/company_info/acmecorp.md 저장
```

## 플랫폼별 특이사항

### Wanted
- 연봉 순위 (상위 N%) 제공
- 채용 중인 포지션 목록
- 유사 기업 추천
- **경력별 제보 연봉**: "경력 예상연봉" > "자세히 보기" 클릭 시 모달에서 탭별(2-4년/5-7년/8-10년/10년초과) 제보 내역 확인 가능

### Remember
- 작년 대비 연봉 변화율
- 상세한 연봉 통계

### Saramin
- **상세 페이지 필수**: `/zf_user/company-info/view?csn={csn}` 페이지에서만 전체 정보 추출 가능
- 기업 기본정보: 대표자명, 설립연도, 직원수, 업종, 주소
- 연봉 정보: 평균연봉, 신입 예상연봉(학력별), 경력별 예상연봉
- 복지 정보: 4대보험, 퇴직금, 연차, 각종 지원금, 근무환경
- 면접 후기: 면접 난이도, 면접 질문, 합격 여부
- 기업 태그: 근무환경, 조직문화, 복지 관련 태그
- 재무 정보: 매출액, 영업이익 (공시 기업 한정)
- 국민연금/고용보험 기반 데이터

### TheVC
- 투자 라운드 상세 이력
- 투자자/투자사 정보
- B2G 계약, 국가 R&D, 특허 수
- 스타트업 랭킹
- 관계사 정보
- **스타트업/벤처 기업만 등록됨**

## 참고사항

- 연봉 데이터는 국민연금/건강보험 기반 추정치일 수 있음
- 일부 정보는 플랫폼 로그인 필요
- TheVC는 스타트업/벤처 기업만 등록됨

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
| TURNOVER_CRITICAL | 퇴사율 ≥ 50% | 🚨 즉시 주의 |
| TURNOVER_HIGH | 퇴사율 ≥ 30% | ⚠️ 조직 안정성 검토 |
| SHRINKING_FAST | 순감소 > 20% | ⚠️ 구조조정 가능성 |
| SALARY_LOW | 상위 50% 미만 | ⚡ 연봉 협상 주의 |
| NO_INVESTMENT_DATA | 스타트업 투자정보 없음 | ⚡ 추가 검증 필요 |

### 검증 결과 처리

1. **완성도 < 70%**: 누락 필드 보완 시도
   - 다른 소스에서 재검색
   - 사용자에게 누락 필드 안내

2. **리스크 플래그 발견**:
   - `--fix` 옵션으로 리스크 섹션 자동 추가
   - 사용자에게 주요 리스크 요약 전달

3. **검증 통과**:
   - "✅ 기업정보 검증 완료 (완성도 N%)" 메시지

### 검증 결과 출력 예시

```
📊 {회사명} 기업정보 추출 완료
- 파일: private/company_info/{slug}.md
- 완성도: 86%
- 스타트업: Yes

⚠️ 리스크 플래그:
- 🚨 TURNOVER_CRITICAL: 퇴사율 97% (1년간 57명 퇴사)
- ⚠️ SHRINKING_FAST: 순감소 -43명 (-73%)

📝 누락 필드: 누적 투자금

→ JD 스크리닝 시 조직 안정성 항목에서 자동 반영됩니다.
```

### 스키마 참조

상세 필드 정의: `private/company_info/_schema.md`
