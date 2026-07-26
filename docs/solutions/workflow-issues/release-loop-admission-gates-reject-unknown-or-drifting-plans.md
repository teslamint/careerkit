---
module: release-loop
date: "2026-08-01"
problem_type: workflow_issue
component: plan-gate
severity: high
applies_when:
  - "release-loop progress ledger is missing, corrupt, or carries an unknown schema"
  - "phase state says plan is done but the approved plan is not a tracked durable artifact"
  - "an approved plan can be changed without a seal or approval checkpoint"
  - "branch drift means the current checkout no longer matches the ledger branch"
tags:
  - release-loop
  - plan-gate
  - durable-evidence
  - body-seal
  - corrupted-ledger
  - branch-drift
---

# Release-Loop Admission Gates Reject Unknown or Drifting Plans

## Context

A recorded release-loop phase is only a hint until the ledger, checkout, and plan
lineage match repository reality.

The CLI verbose-mode loop resumed from a corrupt `.release-loop/progress.md`. It used
`schema: v1`, mixed frontmatter and table phases, and pointed to ignored
`.claude/plans/cli-verbose-mode.md`. The table claimed Plan was done and Review was in
progress, but Git contained no durable draft or approval history for that plan.

The same recovery later detected checkout drift. The ledger named
`feat/cli-verbose-mode`, while the live checkout had moved to another branch. Work
stopped until the recorded branch was restored.

## Guidance

Validate four admission gates before trusting a recorded phase.

1. Validate the ledger schema exactly.
   Reject unknown schemas. Rebuild corrupt state from Git evidence rather than
   guessing how an older field maps to the current contract.
2. Validate the live checkout.
   The checked-out branch must match the ledger branch. Block before any edit or
   commit when it differs.
3. Validate every artifact pointer.
   The spec and plan paths must exist. A Plan-complete claim additionally requires
   the plan to be Git-tracked and present in a commit.
4. Validate the approval chain.
   Git history must show a draft plan commit, a separate user-approved commit, and
   a matching canonical `body_seal`. Later body changes require user-accepted
   interactive deepening and a new seal.

If any gate fails, resume at the highest phase supported by durable evidence. Rewrite
the ledger using the current schema and record the reason for moving backward.

## Why This Matters

`.release-loop/` is gitignored local state. It supports recovery but cannot prove its
own correctness. A loose file pointer can resolve to an ignored document with no
reviewable approval history. A valid-looking phase can also refer to a branch that is
no longer checked out.

The repaired loop became trustworthy only after these events:

- `5405643b` committed a tracked draft plan.
- `d9d26271` recorded user approval and the canonical body seal.
- `4be35530` recorded user-approved deepening and a replacement seal.
- The ledger blocked on branch drift before another commit was made.

The same rule applies at Ship. A recorded merge command is preparation evidence, not
proof that CI passed, approval exists, or the PR merged.

## When to Apply

- Resuming any interrupted release loop
- Migrating or repairing an older progress ledger
- Continuing from a manually rebuilt ledger
- Resolving a plan path under an ignored directory
- Detecting that another task changed the live checkout
- Verifying Plan, Review, or Ship completion after context loss
- Handling any post-approval plan-body change

## Examples

### Reject an unsafe resume

Do not resume Review from this evidence alone:

```text
schema: v1
branch: feat/example
Plan: done — .claude/plans/example.md
Review: in_progress
```

Verify the schema, branch, Git tracking, draft commit, approval commit, and seal. If
the plan lacks durable approval evidence, rebuild the ledger and return to Plan.

### Block branch drift

If the ledger records `feat/example` but `git branch --show-current` returns another
branch, record a blocker and stop. Do not carry dirty changes across branches without
explicit authorization.

### Distinguish preparation from execution

A `final_action.command` does not prove merge authorization or execution. Verify the
PR head, CI checks, review threads, first-hand user approval, and merged PR state
before marking the final action executed.

## Related Evidence

- `docs/retros/2026-07-25-local-llm-screening-guard-retro.md` records the earlier
  missing-ledger and unresolved-plan-pointer risk.
- `docs/deviations/2026-07-26-curated-export-refresh-posture-001.md` records
  post-approval plan drift and byte-exact restoration.
- `docs/solutions/workflow-issues/unverified-storage-semantics-in-mutation-matrix.md`
  covers assumption verification inside plans. This guidance covers admission to a
  release-loop phase.
