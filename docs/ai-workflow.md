# AI Workflow

Claude Code와 함께 이력서 관리 및 구직 활동을 진행하는 방법입니다.

## Claude Code Skills

이 프로젝트에는 구직 활동을 지원하는 Claude Code 스킬이 포함되어 있습니다.

### 사용 가능한 스킬

| 스킬 | 설명 | 출력 |
|------|------|------|
| `/extract-company-info` | 회사 정보 추출 | `private/company_info/<company>.md` |
| `/extract-recruitment-info` | 채용 정보 통합 추출 (회사 + JD) | `private/company_info/` + canonical JD record |
| `/extract-job-posting` | 채용공고 추출 | `(platform, job_id)` canonical JD record |
| `/jd-screening` | JD 적합성 분석 | canonical record screening revision |
| `/jd-batch` | 여러 채용공고 배치 처리 및 상태 갱신 | canonical records + runtime results |
| `/resume-build` | 이력서 빌드 및 검증 | `private/build/` |
| `/verify-interview-content` | 면접 준비 문서 사실 검증 | (inline output) |

## 구직 워크플로우

### 0. 자동 파이프라인 (권장)

`career-jobs run auto`는 설정 기반 검색 결과를 같은 실행에서 추출, 스크리닝,
분류 단계로 전달합니다.

```bash
# 전체 자동화 (검색 → JD 추출 → 스크리닝 → 분류)
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto

# 검색 없이 URL 파일로 실행
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls private/jd/runtime/search/requests/search_YYYYMMDD_HHMM.txt

# 기존 JD만 스크리닝/분류
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --screening-only --from-urls private/jd/runtime/search/requests/search_YYYYMMDD_HHMM.txt

# 저장된 미완료 URL 집합 재처리
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --resume

# 미리보기 모드 (파일 저장/이동 최소화)
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --dry-run

# 검색만 실행
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --search-only

# 실행 수 제한 / 스크리닝 타임아웃 / 분류 생략
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --max-urls 10 --llm-timeout 180 --no-classify
```

운영 명령:

```bash
# 기업정보 검증 및 리스크 섹션 갱신
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file <slug>.md --fix

# 설정 기반 검색과 런타임 큐 상태
UV_CACHE_DIR=.uv-cache uv run career-jobs search run --json
UV_CACHE_DIR=.uv-cache uv run career-jobs queue status --json
```

#### 중복 JD 검색 범위

중복 검사는 canonical 저장소에서 `(platform, job_id)` 복합 키로 수행합니다.
판정, 지원 상태, 공고 상태는 서로 독립적인 메타데이터 축이며 폴더 이동으로
표현하지 않습니다.

출력 파일:
- 실행 결과: `private/jd/runtime/auto/results/auto_<run_id>.json`
- TheVC 보완 큐: `private/jd/runtime/company_enrichment/thevc.txt`
- 파생 요약: `private/jd/derived/screening-summary.md`

### 1. 회사 리서치

```
/extract-company-info [회사 URL 또는 이름]
```

출력 예시:
```markdown
# 회사명

## 기본 정보
- 설립: 2020년
- 규모: 50-100명
- 산업: 핀테크

## 기술 스택
- Backend: Python, FastAPI
- Frontend: React, TypeScript
...
```

### 2. 채용공고 분석

```
/extract-job-posting [채용공고 URL]
```

지원 사이트:
- wanted.co.kr
- rememberapp.co.kr
- saramin.co.kr
- jobkorea.co.kr

### 3. 적합성 스크리닝

```
/jd-screening [채용공고 파일]
```

분석 결과:
- 필수 요건 매칭률
- 우대 사항 매칭률
- 지원 추천 여부
- 강조할 경험 포인트

참고:
- 자동 파이프라인의 스크리닝은 LLM 호출(Claude CLI 우선, 실패 시 Codex CLI fallback)로 수행됩니다.
- LLM 실패 시 기본 판정은 `지원 보류`로 처리됩니다.

## 지원 진행 상태 기록

CLI와 로컬 콘솔 모두 지원 진행 상태와 이력을 기록하고 조회할 수 있습니다.

```bash
# 현재 지원 상태와 이력 조회
UV_CACHE_DIR=.uv-cache uv run career-jobs record show wanted:<job-id> --json

# 지원 이벤트 추가
UV_CACHE_DIR=.uv-cache uv run career-jobs record set-status wanted:<job-id> \
  --application-status applied \
  --application-note "지원서 제출"

# 같은 상태를 다시 기록
UV_CACHE_DIR=.uv-cache uv run career-jobs record set-status wanted:<job-id> \
  --application-status interview \
  --application-note "2차 기술 면접"

# 명시 시각으로 정정 이벤트 추가
UV_CACHE_DIR=.uv-cache uv run career-jobs record set-status wanted:<job-id> \
  --application-status rejected \
  --application-status-updated-at <iso-8601> \
  --application-note "최종 결과 수신"
```

`record show --json`은 현재 상태 필드와 함께 `application_history`를 반환합니다.
`storage preflight --json`은 지원 상태 타임스탬프를 개별 키 없이 집계만 출력합니다.

로컬 콘솔도 같은 파이프라인을 사용합니다.

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs console serve --host 127.0.0.1 --port 8765
```

콘솔의 상태 저장은 `PATCH /api/jobs/<platform>/<job-id>/application-status`만 허용합니다.
쓰기 요청은 루프백 Host와 정확히 일치하는 `Origin` 헤더가 필요합니다.

## 스크리닝 기준 설정

`.claude/skills/jd-screening/SKILL.md`에서 개인 기준을 설정합니다:

```markdown
## 스크리닝 기준

### 필수 조건
- 백엔드 포지션
- 연봉 N천만원 이상
- 서울/판교 근무

### 우대 조건
- Python/FastAPI 사용
- 스타트업 환경
- 리모트 가능
```

## 이력서 맞춤화 워크플로우

### 1. 기본 이력서 생성

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build job base  # 기준 이력서 생성
```

### 2. 타겟별 오버라이드 생성

```bash
mkdir -p private/overrides/<target>/profile
cp private/profile/summary-job.md private/overrides/<target>/profile/
# summary-job.md를 JD에 맞게 수정
```

### 3. 타겟 이력서 빌드

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build job full --target <target>
```

### 4. 변경사항 확인

`private/build/resume-job-notes.md`에서 기본 이력서와의 차이를 확인합니다.

## 디렉토리 구조

```
resume/
├── private/              # 개인 데이터 (gitignored)
│   ├── company_info/     # 회사 정보 DB
│   │   └── <company>.md
│   └── jd/
│       ├── records/      # platform + job ID canonical records
│       ├── config/       # 사용자 작성 검색/스크리닝 설정
│       ├── runtime/      # queue, resume state, 실행 결과
│       └── derived/      # 재생성 가능한 인덱스와 요약
├── example/
│   └── interview/        # 면접 준비 시트 예제
└── .claude/
    └── skills/           # Claude Code 스킬
```

## JD 파이프라인 아키텍처

### 저장소와 로컬 콘솔

- 원본 레코드: `private/jd/records/<platform>/<job-id>/`
- 사용자 설정: `private/jd/config/`
- 실행 복구 상태: `private/jd/runtime/`
- 삭제 가능한 인덱스·요약: `private/jd/derived/`

판정, 지원 상태, 공고 상태는 서로 다른 메타데이터 축입니다. 판정이나
상태 변경은 파일 이동이 아니라 동일 레코드의 원자적 메타데이터 갱신으로
처리합니다. 모든 내부 조회와 큐 상태 갱신은 플랫폼과 공고 ID를 함께
사용합니다.

```bash
# 공고 ID 검색 → 결과 선택 → JD/스크리닝 상세
UV_CACHE_DIR=.uv-cache uv run career-jobs console serve --host 127.0.0.1 --port 8765
```

콘솔은 `127.0.0.1`에만 바인딩됩니다.
현재 브랜치 시점에는 조회 중심 도구입니다.
Markdown은 실행 가능한 HTML로 렌더링하지 않습니다.

주요 `careerkit.jobs` 변경 지점:

| 모듈 | 역할 |
|------|------|
| `src/careerkit/jobs/application/automation.py` | 전체 파이프라인 오케스트레이션 |
| `src/careerkit/jobs/application/config.py` | 정규화된 검색 설정 |
| `src/careerkit/jobs/application/search.py` | 검색·중복 제거·최종 cap |
| `src/careerkit/jobs/adapters/platforms/` | 플랫폼별 요청·응답 변환 |
| `src/careerkit/jobs/adapters/storage/file_records.py` | canonical record 저장소 |
| `src/careerkit/jobs/application/screening.py` | 스크리닝 규칙과 게시 |
| `src/careerkit/jobs/application/pipeline.py` | 상태·분류 흐름 |

## 팁

1. **스크리닝 자동화**: 여러 채용공고를 한번에 분석하여 우선순위 결정
2. **이력서 버전 관리**: Git으로 타겟별 변경사항 추적
3. **면접 준비**: 스크리닝 결과 기반으로 예상 질문 준비
