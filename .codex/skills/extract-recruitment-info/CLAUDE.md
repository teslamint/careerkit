# Recruitment Extraction Contract

이 디렉토리의 스킬은 회사 정보와 canonical JD record를 함께 다룬다.

- 회사 정보는 `private/company_info/`가 source of truth다.
- JD는 `(platform, job_id)` 키와 `JDRecordRepository`가 source of truth다.
- runtime queue/result는 `private/jd/runtime/`, 재생성 가능한 view는
  `private/jd/derived/`에 둔다.
- canonical record의 manifest/content revision을 직접 편집하지 않는다.
- 페이지나 기존 데이터에 없는 기술, 역할, 성과를 추가하지 않는다.

배치 실행은 `UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls <url-file>`을
우선한다.
