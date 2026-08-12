---
module: careerkit.jobs
date: 2026-08-11
problem_type: best_practice
component: external-posting-status-checker
severity: high
applies_when:
  - "A probe, parser, or heuristic classifies content fetched from an external site (HTML pages, JSON error bodies, redirects)"
  - "Its tests run only against hand-built fixture payloads"
  - "A misclassification would mutate canonical records or feed a downstream decision"
symptoms:
  - "Fixture suite fully green while the live run misclassifies real responses"
  - "A regex matches an unrelated occurrence of the target key earlier in the real payload"
  - "A marker-string match silently never fires because the live body is encoded differently than the fixture"
root_cause: "fixtures encode the author's assumption of the payload shape, not the payload"
tags:
  - live-smoke
  - fixture-blindness
  - external-input
  - regression-testing
  - dry-run
---

# Live Smoke Verification for External-Content Heuristics

## Context

The closed-posting checker (2026-07-19/20 cycle, PR #32) classifies job postings
by parsing third-party pages and API error bodies. Every probe had
happy/edge/error fixture tests, unit review, and a final branch review — all
green. A read-only live dry-run immediately afterwards found two
misclassification bugs the entire pipeline had passed through:

1. **Anchor-less key match.** A page-status regex (`"status":"(\w+)"`) was
   verified against a fixture shaped like the target object. The real page,
   fetched with the client's Chrome-like User-Agent, embeds react-query
   dehydrated state whose `"status":"success"` entries precede the real field —
   the first match won and an open posting classified as closed. Under
   `--apply` this would have wrongly flipped a canonical record.
2. **Encoding mismatch on a marker string.** A Korean marker
   (`비공개된 채용 공고`) was matched as a raw substring, as in the fixture. Live
   error bodies arrive `\uXXXX`-escaped, so the marker never matched, every
   probe returned indeterminate, and the platform's circuit breaker tripped —
   closed-posting detection was silently dead for that platform.

Both fixtures were *plausible*; both were wrong about the real payload. See
[a-guard-inherits-the-continue-branches-above-it](../logic-errors/a-guard-inherits-the-continue-branches-above-it.md)
for the same epistemic trap in loop control flow: a passing test proves the
predicate, not the reality it claims to model.

## Guidance

- After fixture tests go green, run one **read-only live smoke** against real
  endpoints before trusting any external-content heuristic: for this component,
  `career-jobs record check-closed` (dry-run default) scoped with `--platform`
  to the platforms just touched.
- The smoke must include at least one **known-positive and one known-negative
  subject** (e.g. a posting known open and one known closed/removed), so both
  branches of the classifier execute against reality.
- Treat an all-indeterminate live result as a failure signal, not caution
  working as intended — bug 2 above surfaced exactly as `unknown` counts and a
  tripped circuit breaker.
- Build fixtures **from captured live payloads** (browser-trace capture,
  `api-spec/samples/`, or a saved curl response), not from memory of the shape.
  When a live bug is found, promote the offending real payload into the fixture
  suite as the regression test.
- Scope the live smoke to **read-only probes**: dry-run flags on, no `--apply`,
  never through a persistence path.
  [unverified-storage-semantics-in-mutation-matrix](../workflow-issues/unverified-storage-semantics-in-mutation-matrix.md)
  records a live verification that destroyed a real record's screening revision
  unrecoverably — live verification of *mutation* belongs to disposable
  fixtures, live verification of *classification* belongs to read-only smoke.

## Why This Matters

Fixtures encode the author's model of the payload; the heuristic is being
tested against the assumption it was built from, so structural blind spots are
invisible by construction. Real payloads vary on axes fixtures rarely cover —
User-Agent-dependent rendering, encoding of non-ASCII text, surrounding noise
that shares key names. Both bugs here shipped past TDD, per-unit review, and a
final branch review; the only step that could catch them was contact with the
real response, at the cost of one CLI invocation.

## When to Apply

- A new platform probe, scraper, or error-body parser lands.
- An existing heuristic's endpoint, headers, or client User-Agent changes
  (rendering can change with it).
- A live run of a batch classifier reports an unexpected indeterminate spike —
  suspect encoding/shape drift before suspecting the remote service.

## Examples

- Anchor fix for bug 1: search for the enclosing object key
  (`"openingsInfo"`) first, then match `"status"` only within that window —
  prefer structured anchors over bare key regexes, per
  [saramin-mobile-api-integration-patterns](./saramin-mobile-api-integration-patterns.md)
  ("prefer structured data over regex on surrounding HTML").
- Encoding fix for bug 2: unicode-unescape the body before marker matching;
  the regression fixture is the verbatim escaped live body, not re-encoded
  Korean.
- Complementary boundary: when the live payload still defies classification,
  return indeterminate and mutate nothing —
  [treat-unknown-as-a-batch-mutation-safety-boundary](./treat-unknown-as-a-batch-mutation-safety-boundary.md)
  governs that half; this doc governs proving the classifier against reality.
