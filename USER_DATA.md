# User Data Setup

개인 데이터를 설정하는 방법입니다.

## 개요

이 프로젝트는 `example/` 디렉토리에 예제 데이터를 제공합니다. 개인 이력서를 만들려면 `private/profile/`과 `private/companies/` 디렉토리에 자신의 데이터를 생성해야 합니다.

## 디렉토리 구조

```
resume/
├── private/              # 개인 데이터 (gitignored)
│   ├── profile/          # 개인 프로필 (생성 필요)
│   ├── companies/        # 경력 정보 (생성 필요)
│   ├── overrides/        # 타겟별 오버라이드 (선택)
│   ├── build/            # 생성된 파일
│   └── variant_config.json
└── example/              # 예제 (참고용)
```

## Step 1: 프로필 생성

```bash
mkdir -p private/profile
```

### 필수 파일

#### contact.md

```markdown
# Contact

- Name: 이름
- Email: email@example.com
- GitHub: https://github.com/username
```

#### summary-public.md (포트폴리오용)

```markdown
# 이름

Senior Backend Engineer

## Summary

자기소개 텍스트...
상세한 경력 요약과 강점...
```

#### summary-job.md (지원용)

```markdown
# Summary

Backend Engineer

## Summary

간결한 자기소개...
```

#### skills-public.md / skills-job.md

```markdown
# Key Strengths

- 첫 번째 강점
- 두 번째 강점

## Tech Stack

### Languages
- Python (FastAPI, Django)
- JavaScript/TypeScript
```

#### education.md

```markdown
# Education

## 대학교

- 전공 학사
- 2010.03 - 2016.02
```

### 선택 파일

- `awards.md` - 수상/자격증
- `languages.md` - 언어 능력

## Step 2: 경력 정보 생성

```bash
mkdir -p private/companies/<company>/projects
```

### profile.md

```markdown
# 회사명

## Overview
- Period: 2021.03 - 현재
- Role: 백엔드 엔지니어
- Employment: 정규직
<!-- public-only:start -->
- Position: 시니어
- Department: 개발팀
<!-- public-only:end -->

## Summary
<!-- public-only:start -->
상세한 요약 (포트폴리오용)
<!-- public-only:end -->
<!-- job-only:start -->
간결한 요약 (지원용)
<!-- job-only:end -->

## Key Responsibilities
<!-- public-only:start -->
- 상세 책임 (메트릭 포함)
<!-- public-only:end -->
<!-- job-only:start -->
- 간결한 책임
<!-- job-only:end -->

## Tech Stack
- Python, FastAPI, MySQL
```

### projects/*.md

```markdown
# 프로젝트명

## Overview
- Period: 2021.03 - 2021.12
- Type: 신규 개발

## Summary
프로젝트 요약

## Tech Stack
- Python, FastAPI

## Key Responsibilities
<!-- public-only:start -->
- 상세 책임
<!-- public-only:end -->
<!-- job-only:start -->
- 간결한 책임
<!-- job-only:end -->

## Achievements
<!-- public-only:start -->
- **성과 제목**: 상세 설명 (수치 포함)
<!-- public-only:end -->
<!-- job-only:start -->
- **성과 제목**: 간결한 설명
<!-- job-only:end -->
```

## Step 3: 빌드 설정 수정

`private/variant_config.json` 수정:

```json
{
  "public": {
    "companies": ["company1", "company2"],
    "include_certificates": true,
    "company_detail": {}
  },
  "job": {
    "companies": ["company1", "company2"],
    "include_certificates": false,
    "company_detail": {"company2": "summary"}
  }
}
```

## Step 4: 빌드 테스트

```bash
# 포트폴리오용
UV_CACHE_DIR=.uv-cache uv run career-resume build public all

# 지원용
UV_CACHE_DIR=.uv-cache uv run career-resume build job all
```

## Variant 태그 규칙

**올바른 형식:**
```markdown
<!-- public-only:start -->
포트폴리오용 콘텐츠
<!-- public-only:end -->

<!-- job-only:start -->
지원용 콘텐츠
<!-- job-only:end -->
```

**잘못된 형식 (인식 안됨):**
```markdown
<!-- variant:public -->  ❌
<!-- /variant:public --> ❌
```

## Git 설정

개인 데이터는 `/private/` 디렉토리에 저장되며, `.gitignore`에 이미 포함되어 있습니다:

```gitignore
/private/
```

## 팁

1. **예제 참고**: `example/` 디렉토리의 파일 형식을 참고
2. **Variant 테스트**: 두 variant 모두 빌드하여 차이 확인
3. **점진적 추가**: 최근 경력부터 추가하며 테스트
