---
module: jd-pipeline-planning
date: 2026-07-22
problem_type: workflow_issue
component: release-loop-planning
severity: high
symptoms:
  - "Plan mutation matrix asserted screening revisions are append-only; live verification replaced (deleted) the prior screening document of a real record"
  - "Four review passes (spec review, plan self-review, two unit reviews) accepted the storage-semantics claim without reading the storage code"
  - "Compensation after the fact could restore verdict metadata only — the destroyed content revision was unrecoverable (private/ is gitignored)"
root_cause: "Mutation-matrix rows asserted storage semantics without a file:line citation, and Assumption Recheck only re-runs claims the spec happened to retain"
resolution_type: process_change
applies_when:
  - "Writing a Mutation/failure-state matrix that claims how a store persists, replaces, or retains data"
  - "Planning a live verification that publishes through a real persistence path"
  - "Running Assumption Recheck on a plan whose matrix makes claims the spec never retained"
tags:
  - mutation-matrix
  - assumption-recheck
  - storage-semantics
  - live-verification
  - jd-records
---

# Unverified storage semantics in a mutation matrix destroy real data

_The affected record's platform identifier was redacted on 2026-07-25 — see
`docs/solutions/conventions/private-identifiers-in-tracked-lifecycle-docs.md`._

## Context

2026-07-22 release-loop (PR #33): the plan's mutation matrix for a live screening
verification stated the post-state as "새 스크리닝 revision 추가(append-only)" and the
rollback row as "기존 revision이 이력으로 보존". In reality
`JDRecordRepository.update_screening_result` calls `_cleanup_stale_revisions`
(`src/careerkit/jobs/adapters/storage/file_records.py:190,452-461`), which keeps only the
latest revision — the store is latest-only. The live run on the U4 live-verification record deleted the
prior 지원 비추천 screening document; only verdict metadata could be restored afterward.
Deviation record: `docs/deviations/2026-07-22-u4-live-verification-revision-replacement.md`.

## Guidance

- Every mutation-matrix row that asserts storage semantics (append vs replace, retention,
  idempotency) must carry a file:line citation to the code implementing that behavior,
  verified by reading it at plan time.
- Assumption Recheck must cover matrix-critical claims, not only the assumptions the spec
  retained — a matrix row is a live assumption even when the spec never stated it.
- Treat any live verification that publishes through a real persistence path as destructive
  to existing content unless the code proves otherwise; select targets (operationally inert
  records) accordingly.

## Why This Matters

Four independent review passes propagated the append-only claim because each assumed a
prior pass had grounded it. A single grep would have falsified it. The cost was real,
unrecoverable user data (gitignored `private/` content), discovered only after the mutation.

## When to Apply

At plan authoring (matrix rows), at plan Assumption Recheck, and before any live
verification that writes through `JDRecordRepository` or similar stores.

## Examples

Wrong matrix row: "rollback: revision은 append-only라 롤백 없음" (no citation).
Right matrix row: "rollback: 저장소는 latest-only(`file_records.py:452-461` —
`_cleanup_stale_revisions`)이므로 기존 revision은 파괴됨; 보상은 verdict 메타데이터 복원뿐."
