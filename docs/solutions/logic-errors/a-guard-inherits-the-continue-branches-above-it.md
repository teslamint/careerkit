---
module: careerkit.jobs
date: 2026-07-25
problem_type: logic_error
component: screening-evidence-checks
severity: high
symptoms:
  - "A correct guard with a passing test never rejects the input it was written to reject"
  - "A malformed table header is silently skipped while its data rows validate cleanly"
  - "A two-state flag reports False on a path that has no answer, clearing state another path set"
root_cause: guard placed after a continue branch that skips the very input it validates
resolution_type: code_fix
related_components:
  - screening-publication-gate
  - jd-record-repository
tags:
  - guard-placement
  - control-flow
  - parser-contract
  - tri-state-flag
  - review-residual
---

# A Guard Inherits the `continue` Branches Above It

## Problem

`parse_match_table` in `src/careerkit/jobs/application/evidence_checks.py` enforces
the screening document's fixed 4-column matching-table contract. PR #34 review
round 2 (commit `8c2c8d5`) added the exact-count guard the contract calls for:

```python
if len(cells) > 4:
    return [], "매칭 표 컬럼 초과"
```

The predicate is right, the rationale in its comment is right, and the test added
alongside it — `test_rejects_a_row_with_a_fifth_column` — passed. The guard was
still dead for the case that mattered, because it sat one line *after* this
branch:

```python
if cells[0] in {"요건", "JD 요건", "JD 요구사항"}:
    continue
```

A header row hit the `continue` and never reached either count check. A two- or
five-column header therefore shaped a table whose data rows then passed as four
columns — the exact shape the guard existed to reject. The defect survived seven
review rounds and surfaced only in round 9 (`#3649923636`).

## Symptoms

- A guard with a passing test does not fire on real malformed input.
- The malformed element is one the loop treats specially (header, separator,
  blank line, comment) rather than the element the author pictured.
- The bug report arrives from a reviewer reading control flow, not from a test.

## What Didn't Work

- **Trusting the predicate.** `> 4` versus `< 4` was reviewed carefully and was
  correct; correctness of the condition says nothing about whether it is reached.
- **Trusting the test.** The round-2 test fed a five-column *data* row. It
  exercised the guard through the one path where nothing skips it, which proves
  the predicate and nothing about reachability.
- **Reading the adjacent diff as history.** The round-9 fix was initially
  attributed to a round-8 change that had touched the same function for an
  unrelated reason. `git log -S"매칭 표 컬럼 초과"` shows the guard came from round 2.

## Solution

Move the count checks above the skip, so a header is validated like any other
row (commit `92ef249`):

```python
cells = [cell.strip() for cell in _split_row(line)]
# Count checks run before the header skip: a header is a row too, and
# skipping it first would let a two- or five-column header shape a table
# whose data rows then pass as four columns.
if len(cells) < 4:
    return [], "매칭 표 컬럼 부족"
if len(cells) > 4:
    return [], "매칭 표 컬럼 초과"
if cells[0] in {"요건", "JD 요건", "JD 요구사항"}:
    continue
```

One test per previously skipped branch:
`test_a_five_column_header_cannot_shape_the_table` and
`test_a_two_column_header_cannot_shape_the_table`.

## Why This Works

The loop's notion of "row" is every non-blank, non-separator line in the section.
The author's notion was "data row". A guard placed among branches that encode the
loop's notion must be positioned against that notion, not the author's. Ordering
the checks before every `continue` makes the guard's domain identical to the
loop's domain, which is what "every row is exactly four columns" actually means.

## Prevention

- When adding a guard inside a loop, list the `continue`/`break` branches that
  run before it and state which inputs never reach it. If that list is not empty,
  the guard's scope is narrower than its comment claims.
- Write one test per skipped branch, not one test for the guard. A test that
  reaches the guard through the unskipped path proves the predicate only.
- A guard added to a shared parser belongs above the special-case skips unless a
  specific reason puts it below; "the header is not really a row" is a mental
  model, not a reason the code expresses.
- When a review reply asserts *when* a line was introduced, run `git log -S` on
  that line. The neighboring diff is not evidence of origin.

## Sibling Defect: A Boolean With No Value For "No Answer"

The same review round found a second placement-shaped defect (`#3649923634`, also
fixed in `92ef249`). `verdict_capped` marks a record whose verdict a local
provider capped, and `run_screening` passed a computed boolean into
`update_screening_result` on every publication — including the fallback document
written when every provider fails.

A fallback reflects no provider answer, so it has no basis for either value; it
asserted `False`, clearing a cap no provider had lifted and dropping the record
out of the `queue capped` recovery set. An ordinary `queue rescreen <key>` runs
without `require_strong_provider`, so nothing else stopped it.

The fix made the parameter tri-state — `None` means "no answer, preserve the
stored flag under the record lock" — and re-read the persisted value into the
returned result instead of reporting the local one. Regression test:
`test_fallback_publication_preserves_an_existing_cap`.

The shared rule: a two-state flag forces every writer to assert one of the two
states. When a code path has no basis to assert either, the flag needs a third
state meaning "not mine to say" — otherwise the path silently overwrites what a
path that *did* know wrote.

## References

- Retro: `docs/retros/2026-07-25-local-llm-screening-guard-retro.md`
- Spec: `docs/specs/2026-07-25-local-llm-screening-guard-design.md`
- Commits: `8c2c8d5` (guard introduced), `92ef249` (both fixes)
- Review comments: `#3649923636`, `#3649923634` (PR #34)
