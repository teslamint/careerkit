---
name: extract-job-posting
description: Use when extracting one or more recruitment-platform job postings from URLs.
---

# 채용공고 추출

HTTP extractor를 우선하고 필요한 플랫폼만 브라우저 fallback을 사용한다.

## 실행

```bash
# 단일 URL의 중복 확인과 처리
UV_CACHE_DIR=.uv-cache uv run career-jobs ingest url "<URL>"

# URL 파일 일괄 처리
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls <url-file>
```

지원 플랫폼과 URL 식별 규칙은 [ARCHITECTURE.md](../../../ARCHITECTURE.md)의
플랫폼 추가 절차를 따른다. 새 플랫폼을 추가할 때는 별도 스크립트를 만들지
말고 `careerkit.jobs`의 공통 추출 계층과 composition root에 등록한다.

## 저장 계약

- `JobRecord(platform, job_id, ...)`를 구성하고 `JDRecordRepository.create()`
  또는 기존 자동화 writer로 저장한다.
- 같은 숫자 ID라도 플랫폼이 다르면 별도 레코드다.
- `private/jd/records/` 하위 경로를 수동 생성하지 않는다.
- 회사 정보는 별도 source-of-truth인 `private/company_info/`에 둔다.

추출 실패 시 불완전한 manifest를 게시하지 않는다. 이미 존재하는 레코드는
저장소 조회 결과를 기준으로 중복 처리한다.
