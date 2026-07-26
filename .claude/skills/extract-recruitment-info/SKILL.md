---
name: extract-recruitment-info
description: Use when extracting both company information and a job posting from a recruitment URL or company query.
---

# 채용/기업 정보 통합 추출

회사 정보와 JD는 서로 다른 source-of-truth를 유지한다.

- 회사 정보: `private/company_info/<company>.md`
- JD와 선택적 스크리닝: `JDRecordRepository` canonical record
- 실행 상태 및 보완 큐: `private/jd/runtime/`

## 권장 실행

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls <url-file>
UV_CACHE_DIR=.uv-cache uv run career-jobs company validate --file <slug>.md --fix
```

단일 URL도 가능하면 같은 pipeline을 사용한다. 브라우저에서 내용을 읽어야
할 때에는 실제 페이지에서 확인된 정보만 추출하고, 일반적인 기술 스택이나
성과를 추론하지 않는다.

## 저장 규칙

1. 플랫폼과 원본 공고 ID를 확정한다.
2. 중복은 `(platform, job_id)`로 조회한다.
3. 회사 정보는 검증 후 회사 파일 owner를 통해 저장한다.
4. JD는 canonical repository writer로 게시한다.
5. 스크리닝이 요청된 경우 동일 레코드에 atomic screening update를 수행한다.

manifest, revision directory, SQLite index, summary를 직접 편집하지 않는다.
SQLite와 summary는 canonical records에서 재생성한다.
