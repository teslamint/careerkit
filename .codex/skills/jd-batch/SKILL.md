---
name: jd-batch
description: Use when processing or screening multiple job posting URLs, resuming a batch, or inspecting JD pipeline status.
---

# JD 배치 처리

resume 저장소 루트에서 `uv run python`으로 실행한다.

## 기본 흐름

```bash
# 검색부터 추출, 스크리닝, 상태 갱신까지
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto

# 검색 결과 URL 파일부터 시작
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls private/jd/runtime/search/requests/<file>.txt

# 미완료 실행 재개
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --resume

# 읽기 중심 사전 확인
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --dry-run
```

## 저장 계약

- canonical record는 `(platform, job_id)`로 식별한다.
- JD/스크리닝/판정/상태 변경은 저장소 API 또는 위 자동화 진입점을 통해
  수행한다. physical content revision을 직접 편집하지 않는다.
- queue, search state, auto result는 `private/jd/runtime/`에 둔다.
- SQLite와 summary는 `private/jd/derived/`의 재생성 가능한 view다.
- 판정, 지원 상태, 공고 상태를 폴더 이동으로 표현하지 않는다.

## 완료 확인

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs queue status --json
UV_CACHE_DIR=.uv-cache uv run career-jobs summary rebuild --json
UV_CACHE_DIR=.uv-cache uv run career-jobs screening lint --all
```

오류가 있어도 기존 canonical record를 덮어쓰거나 ID만 보고 다른 플랫폼의
레코드를 갱신하지 않는다.
