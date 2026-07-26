# Claude Code Project Guide

Resume builder & job search automation system. Markdown source → PDF/HTML/TXT output.

> **Full documentation**: [Getting Started](docs/getting-started.md) · [Customization](docs/customization.md) · [AI Workflow](docs/ai-workflow.md)

## Skills Quick Reference

| Skill | Purpose | Output |
|-------|---------|--------|
| `/extract-company-info` | Company info from Wanted/Remember/Saramin/TheVC | `private/company_info/<company>.md` |
| `/extract-recruitment-info` | Combined company + JD extraction | `private/company_info/` + canonical JD records |
| `/extract-job-posting` | JD extraction from recruitment sites | `JDRecordRepository` record keyed by platform + ID |
| `/jd-screening` | JD fit analysis against screening rules | Screening revision in the canonical record |
| `/jd-batch` | Batch process URLs or update record state | Canonical records + runtime results |
| `/resume-build` | Build resume with variant/target options | `private/build/` |

## Auto Pipeline CLI

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto                          # full pipeline
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --from-urls <file>        # skip search
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --screening-only --from-urls <file>  # screen existing JDs only
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --resume                  # retry saved pending URLs
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --max-urls 10             # bound this run
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --llm-timeout 180         # screening timeout
UV_CACHE_DIR=.uv-cache uv run career-jobs run auto --no-classify             # publish screening without classification
```

- `--resume` retries the currently saved pending URL set; it does not restore a legacy per-stage snapshot.
- Duplicate detection uses the compound `(platform, job_id)` key in canonical records.

### Local LLM Screening Env

| Env | Default | Purpose |
|-----|---------|---------|
| `OLLAMA_SCREENING_MODEL` | `gpt-oss:20b` | Ollama model for screening fallback; set `off` to disable the local path (unless `LOCAL_LLM_BASE_URL` is set) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server base URL |
| `OLLAMA_NUM_CTX` | `32768` | Ollama context window (`num_ctx`); must be positive, rejected at resolve time otherwise |
| `LOCAL_LLM_BASE_URL` | (unset) | When set, use the OpenAI-compatible path instead of Ollama |
| `LOCAL_LLM_MODEL` | (unset) | Model name; required when `LOCAL_LLM_BASE_URL` is set |

`--llm-timeout` limits cloud CLI providers (`claude`, `codex`), and
`--local-llm-timeout` limits local HTTP providers (fallback path). When cloud CLIs are
unavailable and screening depends on local fallback, run with
`--local-llm-timeout 300` (and keep `--llm-timeout` at a shorter default such as `120`).

A local provider (`ollama` / `local`) never publishes `지원 추천` — its verdicts cap at
`지원 보류` and the record records `verdict_capped`. Recover them once a stronger provider
is reachable:

```bash
UV_CACHE_DIR=.uv-cache uv run career-jobs queue capped --list       # what is held back
UV_CACHE_DIR=.uv-cache uv run career-jobs queue capped --rescreen   # retry with a stronger provider
```

### Local LLM Model Swap Criteria

A candidate model replaces `gpt-oss:20b` only if, on the same JD set, it (a) satisfies the
4-column output contract, (b) has a `[source:]` resolution failure rate no worse than the
incumbent, and (c) has a strict keyword-violation rate (`unevidenced_keyword_strict`) no
worse than the incumbent. Record the incumbent's rates alongside the candidate's.

An absolute zero bar is unreachable: on documents written by strong providers, strict
keyword violations measure ~6.6% of `충족` rows and fabricated citations ~1.08%. Smaller
models can match a verdict while getting there the wrong way — the 2026-07-25 bench found
gemma4 8B/12B marking absent experience `충족`, which zeroes the gate's input rather than
satisfying it.

Canonical records must be read and written through
`careerkit.jobs.adapters.storage.file_records.JDRecordRepository`.
Do not construct paths inside `private/jd/records/` or edit content revisions directly.
Screening content revisions are latest-only (publishing replaces the previous document).

Before working in a documented area, check `docs/solutions/` for prior learnings and
`CONCEPTS.md` for canonical vocabulary.

## Semantic Filter

`search_config.yaml`의 `semantic_filter.enabled: true`로 활성화. keyword filter(`quick_filter_title`)가 exclude하지 않은 모든 제목에 대해 임베딩 기반 2차 분류 수행 (prefer 포함). Strategy B (backend centroid − non-backend centroid 상대 점수). Backend keyword override: 제목에 backend/백엔드/server/서버가 포함되면 semantic filter를 건너뜀 (false negative 방지). 모델: `jhgan/ko-sroberta-multitask` (기본) / `all-MiniLM-L6-v2` (fallback). 레퍼런스 임베딩은 `private/.cache/`에 pickle 캐싱.

## Critical Gotchas

### 1. Variant Tag Syntax
```html
<!-- public-only:start --> / <!-- public-only:end -->   ✅ CORRECT
<!-- job-only:start -->    / <!-- job-only:end -->       ✅ CORRECT
<!-- variant:public -->    / <!-- /variant:public -->    ❌ WRONG (not filtered, causes duplicates)
```

### 2. Summary Mode Only Reads `## Overview`
`extract_overview()` reads content between `## Overview` and next `## `. Content in `## Summary` or later sections is **ignored**. Place all summary-mode content inside `## Overview` with `job-only` tags.

### 3. Full-Mode Override Requires ALL Project Files
Override is file-level. If a company is in full mode, ALL files under `companies/<company>/projects/` must have overrides. Missing → original (Korean) content leaks through.

### 4. Company Key Case Sensitivity
`config.json` keys must match directory names exactly: `"CompanyB"` not `"companyb"`.

### 5. variant_config.json is Gitignored
Must be created manually: `cp variant_config.example.json private/variant_config.json`

## Guard and Contract Code

Rules distilled from defects in the `careerkit.jobs` publication guards. Every one of
these shipped past a green test suite and was caught only in review, so treat them as
checks to run while writing, not afterwards.

- **Guards allowlist, never denylist.** A check deciding whether model output may be
  published enumerates what is permitted. A denylist admits every label nobody thought
  of: the `fallback` provider passed a local-provider denylist and cleared a cap it had
  never lifted.
- **Reject before any fallback branch.** When a guard rejects a candidate, return it as
  rejected immediately. A lookup below that re-admits it makes the guard decorative — a
  workspace-containment check was undone by a basename fallback three lines later.
- **Fixed-shape contracts check exact counts.** `!= 4`, not `< 4`. A row with a fifth
  column parsed cleanly and hid a citation past the last cell anything read.
- **Change the writer with the reader.** A format this code both parses and rewrites gets
  both sides changed together, plus a round-trip test. Fixing only the parser leaves the
  writer editing the wrong cell — strictly worse than the bug being fixed.
- **New validation covers every entry point, including this application's own output.**
  A rule added inside a service path must be checked against the standalone CLI and
  against documents the code itself generates. `screening validate` began rejecting the
  fallback document `run_screening` had just written, because the service path never
  reaches that branch.
- **Derived values follow the mutation they describe.** A metric reading a document's
  final state is computed after the document is final; a metric counting what the model
  wrote stays before. Say which one it is in a comment — the next reader will otherwise
  "fix" the correct half.
- **Batch aggregation sums.** `dict.update` on per-item telemetry keeps only the last
  item, hiding exactly the failures the telemetry exists to surface. Use `Counter`, or
  count per key. (`Counter.update` sums; plain `dict.update` replaces.)
- **Re-read the authoritative field, never a proxy.** Recovery tallies read the persisted
  flag, not the outcome of a downstream call that can skip for unrelated reasons.
- **Numeric CLI options that slice or index use `_positive_int`.** `--limit -1` sliced
  the whole queue and would have re-published nearly every record.
- **A test must not neutralize what it tests.** A double supplying an empty allowlist, or
  a fake whose shape differs from the real result, hides the bypass being asserted.
- **Changing a tuned threshold's inputs requires re-measuring it.** R5/R6 thresholds come
  from a corpus run. Any change to what counts as a match re-runs that measurement before
  it lands.

Job-search data never becomes a tracked artifact: platform job IDs, company and team
names, employment types, and screening outcomes stay under `private/`. A `.gitignore`
rule does not untrack what is already committed (`git rm -r --cached` does), and a public
remote keeps whatever was already pushed.

### 비식별화 기준 (tracked 문서 작성 시)

`docs/solutions/`, `docs/retros/`, 코드 주석 등 tracked 파일에 스크리닝 사례를
인용할 때 아래 항목을 비식별화한다.

| 비식별화 대상 | 예시 | 대체 표현 |
|---|---|---|
| 플랫폼 레코드 ID | `wanted:123456` | "어느 레코드", "해당 공고" |
| 회사명 | A사 | "어느 스타트업", "해당 회사" |
| 회사별 연봉·매출·인원 수치 | 전사 평균 N,NNN만원, MoM -N% | "하한을 약간 초과", "단기 인원 변동" |
| 포지션명 + 회사 조합 | A사 Backend Engineer | "스타트업 백엔드 포지션" |
| 스크리닝 룰의 개인 재무 수치 | 현재 연봉 N,NNN만원, 하한 N,NNN만원 | "현재 연봉", "수용 하한", "하한 초과/미달" |
| 룰 내 연봉 구간·배수 | N,NNN~N,NNN만원, ×N.N~N.N | "△ 구간", "priority-3 추정 범위" |

스크리닝 룰 파일(`private/jd/config/`)도 untracked이므로, 룰에 포함된 개인
연봉·재무 수치 역시 tracked 문서에 노출하지 않는다.

유지하는 항목:
- 룰 조문 번호 (5장, 1장B④ 등) — 참조용
- 룰의 비개인 임계값 (퇴사율 50%, 경력 상한 ≤ 10년 등) — 일반 기준
- 날짜 (2026-04-25 감사 등) — 이력 추적용
- 패턴 이름 ("과잉 교정", "즉석 임계값 발명" 등) — 교훈 자체

## Resume Content Integrity

Override content must not add technologies, roles, or achievements absent from base `companies/` or `profile/` files.

**Inflation patterns to reject:**
- **Role**: adding 매니저/리드/총괄 when actual role is IC
- **Verb**: 재설계 when actual work was 분리; 전환 when actual was 설계
- **Scope**: Cluster when actual infra was managed services (RDS, ElastiCache); Kubernetes when actual was ECS
- **Architecture**: MSA 전환 when actual was partial service extraction

```bash
# Verify after override edits:
grep -i "kubernetes\|k8s" private/build/resume-job-<target>.md
grep "재설계\|총괄\|리드\|매니저" private/build/resume-job-<target>.md
diff private/overrides/<target>/companies/<company>/profile.md private/companies/<company>/profile.md
```

### Generated Content Integrity (interview sheets, mock interviews, etc.)

When writing company-specific technical experience in AI-generated content:
- State ONLY what `private/build/resume-job-base.md` explicitly says as fact
- Do NOT infer specific technical experiences from general statements (e.g., "used Spring Boot" ≠ "solved JPA N+1 with fetch join")
- When experience is absent, write: "직접 경험은 없지만 ~로 접근하겠다"
- Derived documents (`resume-based-qa.md` etc.) must also be verified against the resume source

**Inference patterns to reject:**
- ❌ "Used Spring Boot → therefore experienced JPA N+1"
- ❌ "Commerce service → therefore designed product-category relationships"
- ❌ "Used JPA → therefore applied fetch join and DTO projection"
- ⭕ "Spring Boot 3/Kotlin 기반 커머스 API 설계·개발" (resume verbatim)

```bash
# Verify generated content against resume:
UV_CACHE_DIR=.uv-cache uv run career-resume verify-content private/jd_analysis/interview/<file>.md
```

## Build Verification

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume build job full --target <target>   # targeted
UV_CACHE_DIR=.uv-cache uv run career-resume build job full                     # base
UV_CACHE_DIR=.uv-cache uv run career-resume build public all                   # public variant
UV_CACHE_DIR=.uv-cache uv run pytest -q  # complete suite
```

## JD Screening

JD screening analyses should follow the user's custom screening rules and output format exactly. Use Korean for verdict labels (e.g., 지원 비추천). Do not truncate or abbreviate the structured output.
