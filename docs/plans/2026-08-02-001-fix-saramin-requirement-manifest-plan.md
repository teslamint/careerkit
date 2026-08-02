---
schema: plan/v1
title: Saramin Requirement Manifest Extraction
type: fix
status: draft
date: 2026-08-02
execution: code
origin: docs/specs/2026-08-02-saramin-requirement-manifest-design.md
---

# Saramin Requirement Manifest Extraction Plan

## Goal

Preserve Saramin requirement structure from detail HTML and decoded body sections. Keep dedicated fields authoritative and restore requirement manifest rows without changing stored records.

## Architecture notes

- Keep Saramin HTML interpretation in `adapters/platforms/saramin.py`.
- Reuse the transformation order from `automation._html_to_text`: convert semantic tags before removing remaining tags.
- Do not import the application helper into the adapter. That would reverse the repository dependency direction.
- Add `extract_jd_body_sections(jd_body: str) -> dict[str, str]` beside the existing Saramin extractors.
- Return only canonical `자격요건` and `우대사항` keys from the new helper.
- Keep `extract_requirement_manifest()` unchanged. The writer must satisfy its line-leading bullet contract.
- Keep `_normalize_canonical_bullets()` unchanged. The fix supplies canonical line-leading bullets upstream instead of splitting inline hyphens.
- Keep dedicated detail fields authoritative per section. Use a body section only when the matching field is empty.
- Use synthetic fixtures. Do not commit platform record identifiers, company names, or screening outcomes.

Known Pattern: `automation._html_to_text()` converts `<li>` elements into `- ` items before tag removal. The Saramin adapter needs the same local behavior for a different ownership boundary.

## Risks and dependencies

- The finite heading set can miss a future Saramin label. Unknown lines stay as content instead of ending capture.
- Synthetic HTML can drift from live pages. Fixtures cover both issue-reported shapes and all approved boundary groups.
- Section fallback can duplicate structured data. Per-section precedence tests prevent replacement of a nonempty field.
- The implementation adds no runtime dependency or network call.

## Assumption Recheck

| Approved claim | Fresh command evidence | Outcome |
|---|---|---|
| Detail-field extraction removes all tags and collapses whitespace. | `sed -n '175,188p' src/careerkit/jobs/adapters/platforms/saramin.py` at `2026-08-02T01:39:39Z` still shows tag replacement followed by `\s+` collapse. | match |
| Saramin automation uses decoded body text only as introduction content. | `sed -n '603,656p' src/careerkit/jobs/application/automation.py` at `2026-08-02T01:39:39Z` still reads requirements and preferences from dedicated fields only. | match |
| The requirement manifest requires a supported bullet at line start. | `sed -n '49p;147,210p' src/careerkit/jobs/application/requirement_manifest.py` at `2026-08-02T01:39:39Z` still shows the line-anchored `BULLET_RE`. | match |
| Automation tests contain no Saramin extraction case. | `rg -n "saramin\|Saramin" tests/jobs/application/test_automation.py` at `2026-08-02T01:41:23Z` exited `1` with no matches. | match |

No contradictions or unavailable evidence block this plan.

## File structure

- `src/careerkit/jobs/adapters/platforms/saramin.py`: preserve semantic HTML boundaries and parse body sections.
- `tests/jobs/platforms/test_saramin.py`: cover adapter-level HTML and section contracts.
- `src/careerkit/jobs/application/automation.py`: select dedicated fields or per-section body fallbacks.
- `tests/jobs/application/test_automation.py`: prove canonical Markdown and requirement manifest behavior end to end.

## Requirements trace

| Spec requirement | Owning units |
|---|---|
| R1: Preserve semantic line and list boundaries. | U1, U3 |
| R2: Extract body fallbacks from recognized headings. | U2, U3 |
| R3: Apply fallback independently per section. | U3 |
| R4: Prove generated Markdown creates manifest parents. | U3 |

## Scenario coverage map

| Scenario | Unit chain | Walking evidence |
|---|---|---|
| S1: Structured detail fields retain requirement items. | U1 -> U3 | `test_saramin_extraction_renders_detail_field_manifest_rows` in U3. Covers S1. |
| S2: Body-only requirements remain screenable. | U2 -> U3 | `test_saramin_extraction_uses_body_sections_per_missing_field` in U3. Covers S2. |
| S3: Existing structured fields remain authoritative. | U2 -> U3 | `test_saramin_extraction_applies_field_precedence_per_section` in U3. Covers S3. |

## U1: Preserve detail-field HTML structure

Execution note: characterization-first
Files:
  Modify: `src/careerkit/jobs/adapters/platforms/saramin.py`
  Test: `tests/jobs/platforms/test_saramin.py`
Interfaces:
  Consumes: `extract_detail_fields(html: str) -> dict[str, str]`
  Produces: `_html_fragment_to_text(value: str) -> str`; unchanged `extract_detail_fields` signature
Test scenarios:
  happy: A field with `<li>` elements returns separate canonical `- ` lines.
  edge: `<br>` and paragraph boundaries preserve newlines but do not invent bullets for plain text.
  error: An empty or malformed detail block returns no new field and raises no new exception.
  integration: n/a — this unit owns the adapter leaf contract.
Steps:
  1. Add `TestDetailPageParsing::test_extract_fields_keeps_scalar_values` and `TestDetailPageParsing::test_extract_fields_ignores_malformed_detail_block` as characterization tests.
  2. Run both characterization tests and confirm they pass before production edits.
  3. Write failing tests `TestDetailPageParsing::test_extract_fields_preserves_semantic_boundaries` and `TestDetailPageParsing::test_extract_fields_does_not_invent_break_bullets`.
  4. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py::TestDetailPageParsing::test_extract_fields_preserves_semantic_boundaries tests/jobs/platforms/test_saramin.py::TestDetailPageParsing::test_extract_fields_does_not_invent_break_bullets -q`; confirm the first test sees one collapsed line.
  5. Add `_html_fragment_to_text()` before `extract_detail_fields()`. Convert `<br>`, paragraph, and list tags before removing other tags.
  6. Route each matched detail value through the helper. Preserve existing scalar-field output.
  7. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py -q` and confirm all adapter tests pass.
  8. Commit: `fix(jobs): Preserve Saramin detail field structure`
Acceptance: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py -q` passes.

## U2: Parse finite decoded-body sections

Execution note: test-first
Files:
  Modify: `src/careerkit/jobs/adapters/platforms/saramin.py`
  Test: `tests/jobs/platforms/test_saramin.py`
Interfaces:
  Consumes: `extract_jd_body(html: str, job_id: str) -> str`
  Produces: `extract_jd_body_sections(jd_body: str) -> dict[str, str]`
Test scenarios:
  happy: `■ 자격요건` and `(1) 우대 사항` create canonical section values.
  edge: Every approved target alias parses. Each non-target boundary group stops capture. Existing bullet markers do not duplicate.
  error: An empty body, a malformed body, or a body without target headings returns `{}`.
  integration: n/a — U3 connects the parsed sections to canonical Markdown.
Steps:
  1. Write failing tests `test_extract_jd_body_preserves_semantic_boundaries`, `test_extract_jd_body_sections_supports_all_target_aliases`, `test_extract_jd_body_sections_stops_at_each_boundary_group`, and `test_extract_jd_body_sections_returns_empty_without_target`.
  2. Parameterize the target-alias test across all six qualification labels and both preference labels from the approved spec.
  3. Parameterize the boundary test across one approved label from each major-duty, work-condition, benefit, hiring-process, and company-introduction group.
  4. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py -k "extract_jd_body" -q`; confirm missing structure or the missing helper causes failure.
  5. Route decoded body HTML through `_html_fragment_to_text()` so list and heading lines remain distinct.
  6. Add `extract_jd_body_sections()`. Accept optional `■` or `(number)` heading prefixes.
  7. Recognize qualification labels `자격요건`, `자격 요건`, `지원자격`, `지원 자격`, `필수요건`, and `필수 요건`.
  8. Recognize preference labels `우대사항` and `우대 사항`.
  9. Stop capture at the approved major-duty, work-condition, benefit, hiring-process, and company-introduction labels.
  10. Convert each nonempty captured line into one `- ` item without duplicating a supported marker.
  11. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py -q` and confirm all adapter tests pass.
  12. Commit: `fix(jobs): Parse Saramin body requirement sections`
Acceptance: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py -q` passes and covers all approved label groups.

## U3: Select fallbacks and prove manifest output

Execution note: test-first
Files:
  Modify: `src/careerkit/jobs/application/automation.py`
  Test: `tests/jobs/application/test_automation.py`
Interfaces:
  Consumes: `extract_detail_fields(html: str) -> dict[str, str]`; `extract_jd_body(html: str, job_id: str) -> str`; `extract_jd_body_sections(jd_body: str) -> dict[str, str]`
  Produces: unchanged `JobsExtractionStage._extract_saramin(url: str, job_id: str) -> tuple[str, str, str]`
Test scenarios:
  happy: Dedicated qualification and preference fields render required and preferred manifest parents. Covers S1.
  edge: Parameterized cases prove both mixed-source directions. Each nonempty dedicated field remains authoritative. Covers S3.
  error: Empty fields plus an empty or malformed body render `정보 없음` without an exception.
  integration: A synthetic page flows through `JobsExtractionStage.extract()`, canonical Markdown, and `extract_requirement_manifest()`. Introduction keeps the same text while semantic line and list boundaries become visible. Covers S1, S2, and S3.
Steps:
  1. Add a synthetic Saramin detail-page builder and mobile-detail URL mapping to `FakeHttpClient` tests.
  2. Write failing tests `test_saramin_extraction_renders_detail_field_manifest_rows`, `test_saramin_extraction_uses_body_sections_per_missing_field`, `test_saramin_extraction_applies_field_precedence_per_section`, `test_saramin_extraction_preserves_intro_content_with_semantic_boundaries`, and `test_saramin_extraction_handles_missing_requirement_sources`.
  3. Parameterize the precedence test for detail qualification plus body preference and body qualification plus detail preference.
  4. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/application/test_automation.py -k "saramin_extraction" -q`; confirm detail-field structure or missing fallback causes empty manifest parents.
  5. Import and call `extract_jd_body_sections()` inside `_extract_saramin()`.
  6. Select `fields["자격요건"]` before `sections["자격요건"]`. Apply the same independent precedence to `우대사항`.
  7. Keep introduction text, benefits, main duties, storage, and public interfaces unchanged. Allow only the approved semantic line and list boundaries in introduction formatting.
  8. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py tests/jobs/application/test_automation.py tests/jobs/application/test_requirement_manifest.py -q`.
  9. Run `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs -q`.
  10. Commit: `fix(jobs): Restore Saramin requirements to manifests`
Acceptance: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py tests/jobs/application/test_automation.py tests/jobs/application/test_requirement_manifest.py -q` and `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs -q` pass. Each S-ID has a manifest-level test.

## Mutation/failure-state matrix

No stateful ceremony in the deliverable; no mutation/failure-state matrix required.

## Carry-forward trigger audit

| Tracker row | Trigger class | What fired it | Disposition |
|---|---|---|---|
| GitHub issue #1 | drift-based | Current Saramin extraction still collapses detail markup and omits decoded-body requirement fallbacks. | Fold into U1, U2, and U3. Leave `_normalize_canonical_bullets()` unchanged because upstream extraction will emit canonical line-leading bullets. |

Audited GitHub open issues at `2026-08-02T01:39:39Z` on commit `1f72860223f75bc83838e5d9aec6030671f9bdb5`: 1 open rows, 1 fired, 0 unobservable.

## Deferred to Follow-Up Work

- Re-extract existing canonical records after this fix. The approved spec excludes record rewrites.
- Populate main-duty fallbacks from decoded body sections. GitHub issue #1 covers qualifications and preferences only.
- Generalize HTML-to-text behavior across platform adapters. The adapter ownership boundary does not justify that refactor here.

## Open unknowns

### Planning-time

None.

### Implementation-time

None. The plan fixes helper signatures, label sets, precedence, fixtures, and verification commands.
