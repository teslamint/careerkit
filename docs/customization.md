# Customization

이력서 빌드 시스템의 커스터마이징 옵션입니다.

## Variant 시스템

두 가지 variant로 이력서를 생성할 수 있습니다:

| Variant | 용도 | 특징 |
|---------|------|------|
| `public` | 포트폴리오/공개용 | 상세 정보, 모든 회사 포함 |
| `job` | 지원용 | 간결함, 최근 경력 중심 |

### Variant 태그

마크다운 파일에서 variant별 콘텐츠를 구분합니다:

```markdown
<!-- public-only:start -->
상세 메트릭과 수치 (커밋 1,200+, DAU 10만+)
<!-- public-only:end -->

<!-- job-only:start -->
간결한 성과 요약
<!-- job-only:end -->
```

**중요**: 태그 형식을 정확히 지켜야 합니다. `<!-- variant:public -->` 같은 형식은 인식되지 않습니다.

## Override 시스템

특정 회사/포지션 지원 시 맞춤형 이력서를 생성합니다.

### 구조

```
private/overrides/
└── <target>/
    ├── config.json         # 설정 오버라이드
    ├── style.css           # 타겟별 CSS (선택사항)
    ├── profile/
    │   └── summary-job.md  # 파일 오버라이드
    └── companies/
        └── <company>/
            ├── profile.md
            └── projects/   # 디렉토리 오버라이드 (전체 교체)
                └── *.md
```

### config.json 예시

```json
{
  "job": {
    "companies": ["techcorp", "startup1"],
    "company_detail": {
      "startup1": "summary"
    },
    "include_awards": false
  }
}
```

### 파일 오버라이드

동일한 경로에 파일을 배치하면 원본 대신 사용됩니다:

```
# 원본
private/profile/summary-job.md

# 오버라이드 (targetco 지원 시)
private/overrides/targetco/profile/summary-job.md
```

### 디렉토리 오버라이드

`projects/` 또는 `achievements/` 디렉토리 전체를 오버라이드할 수 있습니다. `overrides/<target>/companies/<company>/projects/` 디렉토리가 존재하면, 해당 회사의 원본 `projects/` 전체를 대체합니다 (개별 파일 오버라이드가 아닌 디렉토리 단위 교체).

> **주의**: `full` 모드 회사는 모든 프로젝트 파일을 오버라이드해야 합니다. 일부 파일만 오버라이드하면 나머지는 원본(한국어 등)에서 그대로 가져옵니다.

### 빌드

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build job full --target targetco
```

## 회사별 설정

`private/variant_config.json`에서 설정합니다. 먼저 예제를 복사합니다:

```bash
cp variant_config.example.json private/variant_config.json
```

```json
{
  "public": {
    "companies": ["company1", "company2", "company3"],
    "include_certificates": true,
    "company_detail": {"company3": "full"}
  },
  "job": {
    "companies": ["company1", "company2"],
    "include_certificates": false,
    "company_detail": {"company2": "summary"}
  }
}
```

### company_detail 옵션

| 값 | 설명 |
|----|------|
| `full` | 프로필 + 프로젝트 + 성과 모두 출력 |
| `summary` | 프로필 개요만 출력 |

## 생성 콘텐츠 검증 회사 설정

`career-resume verify-content`가 회사명과 계열 관계를 식별하려면 사용자 소유
설정이 필요합니다. 개인 경력 정보는 패키지 코드에 추가하지 말고 다음 파일에
보관합니다.

```bash
cp verify_content_config.example.json private/verify_content_config.json
```

- `company_aliases`: 회사 식별자별 공식명·영문명·약칭 목록
- `parent_company_map`: 자회사·서비스 식별자와 근거를 공유할 모회사 식별자의 매핑
- `technology_keywords`: 현재 이력·포트폴리오·지원 포지션에서 주장 근거를 확인할 기술 용어
- `pattern_keywords`: 일반적 기술 표현에서 임의로 추론하면 안 되는 구체적 패턴·해결 경험

두 keyword 목록은 패키지의 공통 기본값이 아닙니다. 검증 대상인 사용자의 최신
이력·포트폴리오와 지원 포지션을 기준으로 generated config에서 관리합니다.

다른 설정을 일회성으로 사용하려면 `verify-content --config <path>`를 지정합니다.

## 스타일 커스터마이징

### CSS 파일

기본 `style.css`와 `style-short.css`는 `careerkit.resume` 패키지 리소스가
제공합니다. 저장소의 패키지 기본값을 직접 수정하기보다 타겟별 스타일을
사용합니다.

### 타겟별 스타일

`private/overrides/<target>/style.css`를 생성하면 해당 타겟 빌드 시 적용됩니다.

## 빌드 포맷

| 포맷 | 명령어 | 출력 |
|------|--------|------|
| 전체 | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> full` | PDF, HTML, MD, TXT |
| 요약 | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> short` | PDF, HTML, MD |
| Wanted | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> wanted` | TXT (채용사이트용) |
| 경력기술서 | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> career` | PDF, HTML, MD |
| 지원 패킷 | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> packet` | 1페이지 이력서 + 상세 경력기술서 PDF/HTML/MD |
| 모두 | `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> all` | full + short + wanted |
