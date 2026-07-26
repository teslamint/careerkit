---
name: jd-screening
description: Use when the user asks for JD screening, fit analysis, or a support decision for a canonical job record.
---

# JD 스크리닝

`private/jd/config/jd-screening-rules.md`를 기준으로 canonical JD를 분석한다.

## 저장 계약

- 식별자는 반드시 `(platform, job_id)`를 사용한다.
- 입력 JD와 결과 스크리닝은
  `careerkit.jobs.adapters.storage.file_records.JDRecordRepository`로
  읽고 쓴다.
- `private/jd/records/` 아래의 manifest나 content revision 경로를 직접
  만들거나 수정하지 않는다.
- 판정은 `recommended`, `hold`, `not_recommended` 중 하나이며 지원 상태,
  공고 상태와 독립적으로 갱신한다.

## 실행

URL 목록을 처리할 때는 다음 자동화 진입점을 우선 사용한다.

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --screening-only --from-urls <url-file>
```

단일 레코드를 코드에서 처리할 때는 `JobKey(platform, job_id)`로 조회하고
`update_screening_result()`로 본문과 판정을 한 번에 게시한다. 결과는 필수
섹션과 한국어 판정 문구를 유지하고, 이력에 없는 경험을 추론하지 않는다.

## 검증

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs screening lint --all
UV_CACHE_DIR=.uv-cache uv run career-jobs summary rebuild --json
```

요약은 `private/jd/derived/screening-summary.md`에 재생성되며 직접 편집하지
않는다.
