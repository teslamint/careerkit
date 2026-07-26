---
module: ext/native-host
date: 2026-07-30
last_updated: 2026-07-31
problem_type: logic_error
component: screening-worker
severity: high
symptoms:
  - "Badge stays on unscreened state after screening notification fires"
  - "screening_complete push message carries screening_verdict: null"
  - "Three communication-layer fixes fail to resolve a data-layer problem"
root_cause: "completion signal sent without verifying the operation produced a result"
resolution_type: code_fix
related_components:
  - content-script-badge
  - service-worker
  - resume-config-validation
tags:
  - no-exception-not-success
  - completion-signal
  - null-verdict
  - native-messaging
  - config-validation
  - silent-omission
---

# Completion Signal Must Verify Outcome

## Problem

The native messaging host's screening worker sent `screening_complete` after
`screening_stage.screen()` returned, regardless of whether a verdict was
produced. When `screen()` skipped the record (company info missing or
incomplete), it added the item to `metadata.failures` and continued — no
exception raised, no verdict written. The worker read back `screening_verdict:
null` and broadcast it as a successful completion.

The content script's `renderFromRecord()` treated null verdict as "unscreened"
and rendered the ⚪ screening button — identical to the pre-click state. Three
rounds of fixes targeted the communication path (service worker lifecycle,
polling, context invalidation) before diagnostic logging revealed the push
message itself carried null data.

The same defect class appeared in resume configuration validation.
The validator accepted unresolved directory references in some configuration paths.
The builder returned normally but silently omitted the referenced company.

## Symptoms

- Screening notification fires (proves the push path works).
- Badge reverts to ⚪ instead of showing a verdict color.
- Service worker console shows no errors.
- Content script receives `screening_complete` with `screening_verdict: null`.

## What Didn't Work

- **Service worker sendResponse fix** — the callback was unreliable due to MV3
  lifecycle, but fixing it didn't help because the response data itself was wrong.
- **Polling fallback** — polling the lookup endpoint also returned null verdict,
  because the record genuinely had no verdict.
- **Push message broadcast** — the push arrived correctly; the payload was the
  problem, not delivery.

The shared failure mode: all three fixes assumed the data was correct and the
transport was broken. One `console.log` of the push payload would have falsified
every hypothesis in the first round.

## Solution

In `run_screening_worker` (careerkit_host.py), after `screening_stage.screen()`
returns:

1. Check `ScreeningBatch.metadata.failures` for the record's job key.
2. If found, send `screening_failed` with the failure message.
3. If verdict is still null after screening, send `screening_failed`.
4. Send `screening_complete` only when verdict is non-null.

For directory-backed configuration, validate the complete reference surface:

1. Read the base configuration and every target override configuration.
2. Collect keys from every field that references the directory.
3. Reject a case-only mismatch with the expected directory name.
4. Reject a key when no directory matches.

## Why This Works

"No exception" means "the function returned normally," not "the function did
what you expected." `screen()` is designed to skip records with missing
prerequisites and report them in metadata — a normal return. The worker's job
is to translate that outcome into the correct push message type, not to assume
success from the absence of failure.

Configuration keys are references, not optional labels.
Failing before the build prevents a valid process exit from hiding incomplete output.

## Prevention

- **Verify the outcome, not just the return.** When an async worker sends a
  completion signal, check the result field that downstream consumers will
  render. If it's null/empty, the operation did not succeed.
- **Log the payload before debugging the transport.** When a consumer shows
  wrong state after receiving a message, inspect the message content first.
  Three rounds of transport fixes could have been avoided by one log line.
- **Operations that skip silently need callers that check explicitly.** A
  function that reports failures in metadata instead of raising must have every
  caller read that metadata — the "no exception" contract shifts the checking
  burden to the call site.
- **Test every reference path and failure mode.** Cover each configuration
  source, each reference field, case-only mismatches, and missing references.
