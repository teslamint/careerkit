---
module: careerkit.jobs
date: "2026-08-01"
problem_type: best_practice
component: platform-adapters
severity: medium
applies_when:
  - "Adding a new platform adapter that returns HTML instead of JSON"
  - "Writing company_info markdown that must pass parse_company_file roundtrip"
  - "Integrating a platform with anti-bot protections on its primary domain"
tags:
  - saramin
  - html-parsing
  - company-info
  - startup-completeness
  - anti-bot
---

## Context

Saramin (`www.saramin.co.kr`) has an anti-bot gate (Issue #4) that blocks
headless and curl-based access with a 307 meta-refresh to an error page.
The mobile subdomain (`m.saramin.co.kr`) serves the same data without
authentication or cookies, using a server-rendered partial-HTML pattern:
endpoints return JSON with an `innerHTML` field containing pre-rendered
HTML fragments, not structured JSON.

## Guidance

### 1. Probe before designing

A single curl request collapses three unknowns: anti-bot behavior,
response structure, and data availability. Run it with the same UA string
`UrllibHttpClient` sends. The probe result determines whether the design
is viable — not the other way around.

### 2. HTML parsing: prefer structured data over regex

Saramin company-info pages embed JSON-LD (`@type: Organization`) with
`legalName`, `foundingDate`, `numberOfEmployees`, `founder`, `address`.
Use this over regex on surrounding HTML. For search results and detail
pages where no structured alternative exists, use card-boundary splitting
(`data-rec_idx=`) before per-card regex.

### 3. Startup flag: lock `아니오` when investment data is unavailable

`parse_company_file` treats `is_startup=True` as requiring 4 additional
fields (investment_round, investment_total, employee_joined_1y,
employee_left_1y). Saramin provides none of these. Writing
`스타트업 여부 | 예` — or any `STARTUP_POSITIVE_KEYWORDS` like "벤처" —
causes `completeness_score` to drop to 33%, which
`JobsScreeningStage.screen()` rejects as `"company info completeness
below 70"`. Always write `| 스타트업 여부 | 아니오 |` for Saramin-sourced
company info to lock `startup_status_locked=True`.

### 4. Writer/reader contract: round-trip test is mandatory

Any function producing `private/company_info/*.md` must pass:
`format() -> write -> parse_company_file() -> validate_company()` with
`completeness_score >= 70`. This is not a style preference; a file that
fails it silently stalls the screening pipeline for every posting from
that company.

### 5. csn has no persistent storage — accept the re-fetch

`JobRecord` has no metadata field. Rather than adding one (schema version
bump, migration), re-fetch the detail page when company info is needed.
One extra HTTP request per company is cheaper than domain model changes.

## Why This Matters

A company_info file that looks correct to a human reviewer can score 33%
in `validate_company` and block the entire screening pipeline for that
company. The failure is silent — no crash, no error, just a
`"company info completeness below 70"` entry in the screening failure
list that is easy to miss in batch output.

## When to Apply

- Adding any new platform adapter to `careerkit.jobs`
- Writing company_info markdown from any automated source (not just
  Saramin)
- Integrating any external site that may have anti-bot protections
