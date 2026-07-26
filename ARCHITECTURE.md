# Repository Architecture

이 문서는 현재 저장소의 제품 경계, 데이터 권한, 공개 명령과 변경 지점을 정의하는 기준 문서다.
세부 사용법은 [Getting Started](docs/getting-started.md), [Customization](docs/customization.md),
[AI Workflow](docs/ai-workflow.md)를 따른다.

## Product boundaries

```text
careerkit
├── resume                  # 이력서 제품
│   ├── domain              # variant, schema, content rules
│   ├── application         # build/validate/verify use cases
│   ├── adapters            # filesystem and document renderers
│   └── cli.py              # career-resume composition root
├── jobs                    # 채용 자동화 제품
│   ├── domain              # record identity, statuses, verdict rules
│   ├── application         # search, automation, screening, migration
│   ├── adapters            # platforms, storage, config, external tools
│   ├── console             # loopback-only read-only review UI
│   └── cli.py              # career-jobs composition root
└── workspace.py            # proven cross-product workspace discovery only
```

Dependency rules:

- `resume` and `jobs` do not import each other.
- `domain` imports neither application nor adapters.
- Application code depends on protocols and domain values; composition roots select adapters.
- Production package code never imports `templates.*` or mutates `sys.path`.
- Filesystem, HTTP, browser, LLM process, renderer, and SQLite behavior stays in product-owned adapters.
- Shared code is allowed only after both products demonstrate the same ownership need.

## Data authority

| Data | Authority | Owner / access rule |
|------|-----------|---------------------|
| Resume Markdown | `private/profile/`, `private/companies/`, `private/overrides/` | user-authored; `career-resume` reads it |
| Resume outputs | `private/build/` | derived; rebuild with `career-resume` |
| Search and screening policy | `private/jd/config/` | user-authored; check/preview are read-only, apply is explicit |
| JD records | `private/jd/records/<platform>/<job-id>/` | canonical; access through `JDRecordRepository` |
| Queue and run state | `private/jd/runtime/` | mutable runtime state, never canonical content |
| Index and summary | `private/jd/derived/` | disposable views rebuilt from canonical records |
| Public examples | `example/` | synthetic tracked fixtures only |

JD identity is always the compound `(platform, job_id)`. Screening verdict, application status,
and posting status are independent axes; directory movement does not encode them.

## Core flows

### Resume

`career-resume` resolves the workspace, loads Markdown sources, applies variant/target rules, and sends
the normalized document to product-owned output adapters. Markdown remains the source of truth.

### Jobs

`career-jobs` parses one normalized search configuration, selects platform adapters, deduplicates by
compound identity, applies the final cap, and persists only returned seen keys. Canonical records drive
status, screening, index, summary, and console views. External screening receives candidate context only
when the caller explicitly supplies a sanitized context file.

## Public command map and exit codes

Only two installed executables are public: `career-resume` and `career-jobs`.
Use `uv run <command>` in a source checkout; installed wheels invoke the commands directly.

| Product | Command family | Purpose |
|---------|----------------|---------|
| Resume | `career-resume build ...` | public/job/example builds and supported output matrix |
| Resume | `career-resume validate [--example]` | validate source/schema contracts |
| Resume | `career-resume verify-content <path>` | reject unsupported generated claims |
| Jobs | `career-jobs search run` | configured multi-platform search |
| Jobs | `career-jobs run auto ...` | packaged automation sequencing |
| Jobs | `career-jobs ingest url|file ...` | inspect extraction inputs |
| Jobs | `career-jobs screening validate|run ...` | validate or explicitly run screening |
| Jobs | `career-jobs record ...`, `queue ...` | inspect/update canonical state |
| Jobs | `career-jobs storage preflight` | metadata-only corpus readiness proof |
| Jobs | `career-jobs index rebuild`, `summary rebuild` | rebuild derived views |
| Jobs | `career-jobs company validate` | validate human-maintained company information |
| Jobs | `career-jobs console serve` | serve loopback-only read-only review UI |
| Jobs | `career-jobs config check|preview|apply` | inspect or explicitly migrate config |

Exit status contract:

- `0`: command completed or a read-only check is ready.
- `2`: invalid input, rejected/required migration, missing capability, or failed readiness check.
- Diagnostics go to stderr. Stable JSON is provided only where `--json` appears in help and a checked-in
  consumer or acceptance test owns the schema; JSON must contain metadata, never private document bodies.

## One-pass change recipes

### Add or change a JD search condition

1. Add the normalized field and validation in `src/careerkit/jobs/application/config.py`.
2. Apply shared filtering/orchestration in `src/careerkit/jobs/application/search.py` or
   `title_filter.py`.
3. Keep platform-native request values inside `src/careerkit/jobs/adapters/platforms/`.
4. Add config, service, and affected adapter tests under `tests/jobs/`.

### Add a platform

1. Implement the search protocol in `src/careerkit/jobs/adapters/platforms/<platform>.py`.
2. Add URL/identity support without weakening `(platform, job_id)` in
   `src/careerkit/jobs/application/storage_migration.py`.
3. Register the adapter only in the jobs composition root/service registry.
4. Add sanitized request fixtures and pagination/error tests under `tests/jobs/platforms/`.

### Add a resume output format

1. Add the renderer in `src/careerkit/resume/adapters/`.
2. Orchestrate it from `src/careerkit/resume/application/build.py`.
3. Expose the option in `src/careerkit/resume/cli.py`.
4. Extend the synthetic output matrix and installed-wheel resource tests.

### Add or change a status rule

1. Change canonical values/invariants in `src/careerkit/jobs/domain/model.py`.
2. Change transitions in `src/careerkit/jobs/application/status.py` or `pipeline.py`.
3. Persist through `src/careerkit/jobs/adapters/storage/file_records.py` atomically.
4. Test each independent status axis and protected transition.

### Change screening or the console

- Screening orchestration: `src/careerkit/jobs/application/screening.py`.
- Provider/process boundary: `src/careerkit/jobs/adapters/screening/`.
- Local review server/assets: `src/careerkit/jobs/console/`.
- Preserve explicit candidate-context consent, redaction, loopback binding, Host validation, CSP,
  read-only HTTP behavior, and safe text rendering.

## Cutover rules

Until the final cutover, the legacy surface is feature-frozen and remains the real daily-operation path.
The migrated package may touch actual state only through the approved U9 preflight/migration procedure.
Legacy deletion is blocked until tracked and external callers are dispositioned, actual config and corpus
checks pass, a legacy-free clean export passes, and distribution/privacy/equivalence gates are green.
Historical documents under `docs/plans/`, `docs/research/`, and `docs/superpowers/` are evidence, not active
operator instructions.
