---
module: careerkit.jobs
date: "2026-08-01"
problem_type: workflow_issue
component: automation-pipeline
severity: high
applies_when:
  - A stage method adds, removes, or renames a key in metadata it returns
  - A batch result dataclass gains or drops a field consumed by a downstream stage
  - A telemetry dict in stage output is restructured
tags:
  - metadata-contract
  - stage-consumer
  - change-the-writer-with-the-reader
  - pipeline
---

# Stage Metadata Contract Change Requires Consumer Audit

## Context

The `careerkit.jobs` auto pipeline passes batch results between stages
(`ExtractionBatch`, `ScreeningBatch`) via metadata dicts. Each stage
produces metadata; downstream code in `_run_auto()` reads specific keys
to decide resume state, telemetry, and error handling.

## Guidance

When a stage adds, removes, or renames a metadata key, **grep the
metadata variable name across the module to find every consumer before
committing.** One grep is enough:

```bash
grep -n "screened.metadata" src/careerkit/jobs/application/automation.py
```

The same applies to `extraction.metadata`, `completed.metadata`, or any
batch result dict a downstream stage reads.

## Why This Matters

`JobsScreeningStage.screen()` added `company_info_warnings` to its
metadata (commit 96172804). The downstream consumer `_run_auto()` reads
`screened.metadata["failures"]` to decide which URLs stay in
`pending_urls` for `--resume`. Because the new key was not added to that
consumer, warned URLs were silently cleared — the verdict was published
with degraded input, and no retry path existed. A correctness-lane
review caught this as a P1 (commit 8dcd732).

This is the CLAUDE.md rule "Change the writer with the reader" applied
to stage boundaries: the "writer" is the stage producing metadata, the
"reader" is every downstream consumer of that metadata.

## When to Apply

- Adding a new key to a stage's metadata dict
- Changing the semantics of an existing key (e.g., items move from
  `failures` to a new `warnings` key)
- Removing or renaming a key

## Examples

**Before (missed consumer)**:
```python
# In screen(): added company_info_warnings to metadata
metadata["company_info_warnings"] = company_info_warnings
# But _run_auto() only read metadata["failures"] for pending_urls
```

**After (consumer updated)**:
```python
# _run_auto() now reads both failures and warnings
warned_screening_ids = set(
    screened.metadata.get("company_info_warnings", {}).keys()
)
retry_screening_ids = failed_screening_ids | warned_screening_ids
```
