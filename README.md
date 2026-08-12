# careerkit

이력서 관리와 채용 탐색을 함께 다루는 도구 모음. Markdown으로 이력서를 작성해 여러
포맷(PDF, HTML, TXT)으로 빌드하고, 채용공고 수집·스크리닝 파이프라인을 함께 제공합니다.
CLI는 `career-resume`과 `career-jobs` 둘입니다.

코드 개발은 공개 `careerkit` 저장소에서 진행합니다. 이력서 원본, 채용 데이터, 그리고
설계·계획·회고 문서는 별도의 비공개 워크스페이스 저장소에 두며 여기에 커밋하지
않습니다. 기여 절차는 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

## Features

- **Variant 시스템**: `public`(포트폴리오용)과 `job`(지원용) 두 가지 버전 관리
- **Override 시스템**: 지원 회사별 맞춤형 이력서 생성
- **다양한 출력 포맷**: PDF, HTML, Markdown, 채용사이트용 텍스트
- **Claude Code 연동**: AI 스킬로 채용공고 분석 및 이력서 최적화
- **로컬 JD 콘솔**: 플랫폼+공고 ID 기반 검색, 상태 필터, JD/스크리닝 동시 열람

## Quick Start

### Prerequisites

```bash
# macOS
brew install python pandoc uv
pip3 install uv weasyprint

# Ubuntu/Debian
sudo apt-get install python3 pandoc
pip3 install uv weasyprint
```

### 예제로 테스트

```bash
# 예제 데이터로 빌드
UV_CACHE_DIR=.uv-cache uv run career-resume build example all

# 생성된 파일 확인
ls example/build/resume-example*
```

### 개인 데이터 설정

```bash
# 프로필 디렉토리 생성 후 예제 복사
mkdir -p private/profile private/companies
cp -r example/profile/* private/profile/
cp -r example/companies/* private/companies/

# 파일들을 개인 정보로 수정
# private/profile/contact.md, private/profile/summary-*.md 등
```

### 빌드

```bash
# 공개용 이력서 (포트폴리오)
UV_CACHE_DIR=.uv-cache uv run career-resume build public all

# 지원용 이력서
UV_CACHE_DIR=.uv-cache uv run career-resume build job all

# 지원 패킷: 1페이지 이력서 + 상세 경력기술서 PDF
UV_CACHE_DIR=.uv-cache uv run career-resume build job packet

# 특정 회사 타겟
UV_CACHE_DIR=.uv-cache uv run career-resume build job full --target company-name
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
│   ├── build/            # 생성된 파일
│   └── variant_config.json
├── src/careerkit/        # 설치 가능한 제품 패키지
│   ├── resume/           # 이력서 빌드 도메인·어댑터
│   └── jobs/             # JD 검색·저장·스크리닝·콘솔
├── example/              # 예제 데이터
└── docs/                 # 상세 문서
```

## Variant 시스템

마크다운에서 variant별 콘텐츠를 구분합니다:

```markdown
<!-- public-only:start -->
상세 메트릭 (커밋 1,200+, DAU 10만+)
<!-- public-only:end -->

<!-- job-only:start -->
간결한 성과 요약
<!-- job-only:end -->
```

## 문서

로컬 JD 콘솔은 전환 사전 점검 후 실행합니다.

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs storage preflight --json
UV_CACHE_DIR=.uv-cache uv run career-jobs console serve --host 127.0.0.1 --port 8765
```

JD 파일 원본은 `private/jd/records/<platform>/<job-id>/`에 유지되고,
SQLite 검색 인덱스와 요약은 언제든 원본에서 다시 만들 수 있습니다.

- [Getting Started](docs/getting-started.md) - 설치 및 빠른 시작
- [Customization](docs/customization.md) - variant, override 시스템
- [AI Workflow](docs/ai-workflow.md) - Claude Code 스킬 활용

## Claude Code 스킬

구직 활동을 위한 AI 스킬이 포함되어 있습니다:

| 스킬 | 설명 |
|------|------|
| `/extract-company-info` | 회사 정보 추출 |
| `/extract-recruitment-info` | 채용 정보 통합 추출 (회사 + JD) |
| `/extract-job-posting` | 채용공고 추출 |
| `/jd-screening` | JD 적합성 분석 |
| `/jd-batch` | 여러 채용공고 배치 처리 및 재분류 |
| `/resume-build` | 이력서 빌드 및 검증 |

자동화 스크립트:
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto`: 검색 → JD 추출 → 스크리닝 → 자동 분류
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls <file>`: URL 파일 기반 배치 실행
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --screening-only --from-urls <file>`: 기존 JD 대상 스크리닝/분류만 수행
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --resume`: 저장된 미완료 URL 집합 재처리
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --dry-run`: 미리보기 모드
- `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --max-urls 10 --no-classify`: 실행 수 제한 및 분류 생략
- `UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file <slug>.md`: 기업정보 검증

자세한 사용법은 [AI Workflow](docs/ai-workflow.md)를 참고하세요.

## License

MIT License - see [LICENSE](LICENSE)
