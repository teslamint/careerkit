---
module: careerkit.jobs
date: 2026-07-20
problem_type: best_practice
component: external-posting-status-checker
severity: high
applies_when:
  - "A batch command mutates canonical records using untrusted external responses"
  - "A source can be temporarily unavailable, malformed, or redirected"
tags:
  - batch-mutation
  - external-input
  - fail-safe
  - regression-testing
---

# Treat Unknown as a Batch-Mutation Safety Boundary

## Context

The closed-posting checker reads third-party pages and APIs, classifies each
posting, and updates canonical job records. A malformed response or ambiguous
transport result must not be interpreted as evidence that a posting is open or
closed. Because the command processes many records in one run, an unchecked
field access or parser exception can also stop the sweep after earlier records
have already been written.

## Guidance

- Model indeterminate results explicitly and leave the corresponding canonical
  status unchanged.
- Validate the complete untrusted boundary before classification: top-level and
  nested payload shapes, scalar dates, URL schemes and ports, redirect targets,
  retry delays, and bounded error bodies.
- Convert source-specific parsing and transport failures into an indeterminate
  result rather than letting them abort the batch.
- Before review, exercise an adversarial matrix for every platform adapter:
  non-object payloads, null or list-shaped nested values, invalid dates and
  ports, redirects, HTTP-date retry headers, oversized error bodies, and
  negative or non-finite CLI inputs.
- After changing a shared HTTP interface, run validation from a clean candidate
  export so unrelated test doubles and static-analysis behavior are checked.

## Why This Matters

An explicit indeterminate state prevents absence of evidence from becoming a
destructive update. Boundary validation also keeps a single hostile response
from terminating a long-running mutation after partial progress. The clean
candidate check catches integration failures hidden by the current worktree,
including protocol changes that invalidate test doubles and dynamically used
handlers that static analysis cannot see.

## When to Apply

Use this pattern whenever a batch operation updates durable local state from
remote systems that the repository does not control. It is especially important
when adapters have different response shapes or when redirects can cross trust
boundaries.

## Examples

- A missing status field produces an indeterminate result and no record update.
- A redirect to a private or otherwise disallowed target is rejected without
  following it.
- A malformed date is treated as source ambiguity, not as proof of closure.
- A negative retry delay is rejected at the CLI boundary before network work.
