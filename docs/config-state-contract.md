# JD Config and State File Contract

This contract separates user-authored inputs from derived runtime state in the
JD automation pipeline. Runtime code should write only the derived files listed
here.

## User-Authored Inputs

| File | Owner | Readers | Write contract |
|------|-------|---------|----------------|
| `private/jd/config/search_config.yaml` | User configuration | `careerkit.jobs.application.config` through the config file adapter | Runtime code reads this file except for explicitly approved `config apply`. Edit normalized queries and platform settings here. |
| `private/jd/config/jd-screening-rules.md` | User screening policy | Screening and pre-screening flows | Runtime code reads this file only. Edit it manually when changing screening criteria. |

## Canonical Records

`private/jd/records/<platform>/<job-id>/record.json` is the record manifest.
It identifies immutable content revisions containing the required JD Markdown
and optional screening Markdown. The manifest owns three independent axes:
screening verdict, application status, and posting status. Callers use
`JDRecordRepository` rather than constructing physical paths.

The compound `(platform, job_id)` identity is mandatory. A numeric ID alone is
not globally unique and must not be used for queue updates, deduplication, or
detail lookup.

## Derived State

| File | Write owner | Readers | Lifecycle |
|------|-------------|---------|-----------|
| `private/jd/runtime/queue/queue.json` | `JobsPipelineService` | Queue status flow | Items carry both platform and job ID. Status updates match both values. |
| `private/jd/runtime/search_state.json` | `JobsMaintenanceService` | Search flows | Stores deterministic compound seen keys. |
| `private/jd/runtime/auto/pending_urls.json` | `JobsResumeStateService` | Auto resume flow | Pending URL set only; deleted after successful completion. |
| `private/jd/derived/search.sqlite3` | `JDSearchIndex.rebuild()` | Web console and local search | Disposable metadata-only index rebuilt from canonical records. |
| `private/jd/derived/screening-summary.md` | Summary generator | Human review and audits | Disposable deterministic summary rebuilt from canonical records. |

## Recovery and Historical Migration

`career-jobs storage preflight --json` validates an already-canonical
`private/jd/records` tree. It does not ingest `private/job_postings`, and the
post-cutover CLI intentionally has no `storage --activate` command.

The completed flag-day implementation is retained as
`careerkit.jobs.application.storage_migration.StorageMigrator` for audit and
recovery tests. A workspace still using the legacy layout must run that pinned
pre-cutover migration workflow rather than treating a zero-record canonical
preflight as migration readiness. That workflow copies legacy inputs into a
fresh stage and blocks duplicate compound keys, orphan screenings, identity
conflicts, and unresolved ID-only runtime state.

The historical migration sequence was:

1. Freeze search, extraction, screening, and status writers.
2. Run the dry-run against a stage outside both the legacy tree and active
   canonical root. The default `.jd-stage/` at the repository root satisfies
   this separation.
3. Proceed only when `ready: true` and `blockers` is empty.
4. Activate, rebuild the index and summary, then smoke-test ID search and detail.
5. Resume writers only after the smoke test passes.

Rollback is lossless only before any new canonical writes are accepted. During
that window, stop all writers, move the failed canonical root aside for
forensics, restore the pre-cutover code/configuration, and verify the legacy
source hashes from the migration report. If canonical writers have resumed,
do not copy files backward or guess a merge; reconcile records explicitly by
compound key.

Keep the read-only legacy backup for at least 30 days after activation and
until one restore drill reproduces the recorded hashes and state distribution.
Delete it only after the rollback window is formally closed, canonical backups
have been verified, and no unresolved migration finding refers to it. Backup
deletion is always a separate manual operation; the migrator never deletes it.

Derived data is recoverable:

```bash
rm private/jd/derived/search.sqlite3
UV_CACHE_DIR=.uv-cache uv run career-jobs console serve --host 127.0.0.1 --port 8765
```

Server startup reconstructs the index from file records. Summary regeneration
is owned by `career-jobs summary rebuild`; do not hand-edit derived output.
The running console uses an index snapshot. Use **인덱스 갱신 후 검색** (the
`refresh=1` API option) after canonical writers add or update records. A failed
refresh leaves the last complete SQLite index in place and returns a controlled
error instead of publishing a partial index.

## Local Hook Installation

`.codex/hooks.json` is intentionally gitignored. Copy or merge
`docs/examples/codex-hooks.json` into that local file to run canonical
screening lint after edits. The tracked example is validated by the test suite.

## Rules

- Workspace paths come from `careerkit.workspace.WorkspacePaths`; canonical
  record paths are constructed only by the storage adapter.
- User-authored inputs are not mutated by automation.
- Canonical record bodies and metadata are authoritative; SQLite and summaries
  are never authoritative.
- Derived state writes should go through the module owner above so locking,
  timestamping, and status semantics stay centralized.
- Tests should patch these path constants or owner functions to temporary
  directories instead of writing under `private/`.
