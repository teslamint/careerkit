# Getting Started

이력서 빌드 시스템을 사용하기 위한 빠른 시작 가이드입니다.

## Prerequisites

다음 도구들이 설치되어 있어야 합니다:

- **Python 3.8+**
- **uv** - Python 환경 및 명령 실행
- **Pandoc** - Markdown을 HTML로 변환
- **WeasyPrint** - HTML을 PDF로 변환

### macOS

```bash
brew install python pandoc uv
pip3 install uv weasyprint
```

### Ubuntu/Debian

```bash
sudo apt-get install python3 pandoc
pip3 install uv weasyprint
```

## Quick Start

### 1. 예제로 테스트

먼저 예제 데이터로 빌드를 테스트합니다:

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build example all
```

생성된 파일:
- `example/build/resume-example.pdf` - 전체 이력서
- `example/build/resume-example-short.pdf` - 1페이지 요약
- `example/build/resume-example-wanted.txt` - 채용 사이트용 텍스트

1페이지 이력서와 상세 경력기술서 PDF만 함께 확인하려면:

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build example packet
```

추가 생성 파일:
- `example/build/career-description-example.pdf` - 상세 경력기술서

### 2. 개인 데이터 설정

`example/` 디렉토리를 참고하여 개인 데이터를 생성합니다:

```bash
# 프로필 디렉토리 생성
mkdir -p private/profile

# 예제에서 복사하여 수정
cp example/profile/*.md private/profile/
# 이후 private/profile/*.md 파일들을 개인 정보로 수정
```

자세한 설정 방법은 [USER_DATA.md](../USER_DATA.md)를 참고하세요.

### 3. 개인 이력서 빌드

```bash
# 공개용 (포트폴리오)
UV_CACHE_DIR=.uv-cache uv run career-resume build public all

# 지원용
UV_CACHE_DIR=.uv-cache uv run career-resume build job all

# 지원 패킷: 1페이지 이력서 + 상세 경력기술서 PDF
UV_CACHE_DIR=.uv-cache uv run career-resume build job packet
```

## 디렉토리 구조

```
resume/
├── private/              # 개인 데이터 (gitignored)
│   ├── profile/          # 개인 프로필 (연락처, 요약, 기술스택)
│   ├── companies/        # 경력 정보
│   │   └── <company>/
│   │       ├── profile.md
│   │       └── projects/
│   ├── overrides/        # 타겟별 오버라이드
│   │   └── <target>/
│   └── build/            # 생성된 파일
├── src/careerkit/        # 설치 가능한 제품 패키지
│   ├── resume/           # 이력서 빌드 도메인·어댑터
│   └── jobs/             # JD 검색·저장·스크리닝·콘솔
├── example/              # 예제 데이터
└── docs/                 # 문서
```

## 다음 단계

- [JD Automation](#jd-automation-optional) - 채용공고 자동 수집/분석 파이프라인
- [Customization](customization.md) - variant 시스템과 오버라이드 설정
- [AI Workflow](ai-workflow.md) - Claude Code 스킬 활용

## JD Automation (Optional)

이력서 빌드 외에, 채용공고 자동 처리 파이프라인을 사용할 수 있습니다.

JD 원본은 `private/jd/records/<platform>/<job-id>/`의 복합 식별자로
관리합니다. 로컬 콘솔은 외부 서비스 없이 ID 검색, 상태 필터, JD 및
스크리닝 결과 열람을 제공합니다.

```bash
# 검색 설정 예제를 비공개 작업 공간에 복사한 뒤 수정
mkdir -p private/jd/config
cp search_config.example.yaml private/jd/config/search_config.yaml

# 정규화된 role과 플랫폼 설정 검사 (읽기 전용)
UV_CACHE_DIR=.uv-cache uv run career-jobs config check --json

# 레거시 저장 구조를 변경하지 않는 사전 점검
UV_CACHE_DIR=.uv-cache uv run career-jobs storage preflight --json

# 전환 완료 후 로컬 읽기 전용 콘솔 (127.0.0.1:8765)
UV_CACHE_DIR=.uv-cache uv run career-jobs console serve --host 127.0.0.1 --port 8765
```

직무는 `search.role: backend` 한 곳에서만 지정합니다. Wanted, Remember,
GroupBy의 플랫폼별 카테고리 값은 각 어댑터가 변환하므로 비공개 설정에
직접 추가하지 않습니다.

사전 점검 보고서에 차단 항목이 있으면 `--activate`를 실행하지 않습니다.
중복 복합 키, 고아 스크리닝, 플랫폼·ID 충돌은 수동으로 검토해야 합니다.

```bash
# 전체 자동화: 검색 → JD 추출 → 스크리닝 → 분류
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto

# 검색 없이 URL 파일로 실행
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls private/jd/runtime/search/requests/search_YYYYMMDD_HHMM.txt

# 기업정보 검증
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file <slug>.md

# 저장된 미완료 URL 집합 재처리
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --resume
```

### 채용공고 종료 상태 확인

`record check-closed`는 기본적으로 레코드를 변경하지 않는 dry-run으로
실행되며, 사람이 읽기 좋은 고정 형식으로 결과를 출력합니다. 실제로 종료
상태를 반영하려면 `--apply`를 추가합니다.

```bash
# 현재 활성 레코드에서 종료된 공고 확인
UV_CACHE_DIR=.uv-cache uv run career-jobs record check-closed

# 종료 레코드에서 다시 열린 공고 확인
UV_CACHE_DIR=.uv-cache uv run career-jobs record check-closed --recheck-closed
```

기본 출력은 실행 모드와 `apply`, `dry_run`, `changed` 값을 첫 줄에 표시하고,
`closed`, `reopened`, `unknown`, `skipped`, `tripped` 섹션을 항상 출력합니다.
빈 섹션은 `- none`으로 표시됩니다.

자동화나 감사 기록에는 `--json`을 사용합니다. 이 옵션은 사람용 출력과
섞이지 않는 단일 JSON 객체만 stdout에 출력합니다.

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs record check-closed --json
UV_CACHE_DIR=.uv-cache uv run career-jobs record check-closed --recheck-closed --apply --json
```

JSON 객체의 고정 필드는 `command`, `workspace_root`, `workspace_source`,
`mode`, `apply`, `dry_run`, `changed`, `closed_keys`, `reopened_keys`,
`unknown_keys`, `skipped_platform_counts`, `tripped_platforms`입니다.

주요 출력:
- `private/jd/runtime/auto/pending_urls.json`
- `private/jd/derived/screening-summary.md`

콘솔은 시작할 때 인덱스를 재구성합니다. 실행 중 새 레코드가 생기면
**인덱스 갱신 후 검색** 버튼을 눌러 파일 레코드에서 다시 읽습니다.
