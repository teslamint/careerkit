---
title: Saramin Requirement Manifest Extraction
status: draft
date: 2026-08-02
schema: spec/v1
---

# Saramin Requirement Manifest Extraction Design

_Created 2026-08-02._

## Overview

Fix GitHub issue #1 so Saramin qualification and preference text reaches the requirement manifest. Preserve source structure during HTML extraction, then use body sections only when dedicated detail fields are absent.

## User Scenarios

### S1: Structured detail fields retain requirement items

An operator ingests a Saramin posting whose qualification field contains HTML line breaks or list items. The generated JD contains canonical bullet lines, so screening receives required and preferred manifest rows.

### S2: Body-only requirements remain screenable

An operator ingests a Saramin posting without dedicated qualification fields. If the decoded body contains recognized qualification or preference headings, those sections populate the corresponding canonical JD sections.

### S3: Existing structured fields remain authoritative

An operator ingests a posting that contains both dedicated fields and body sections. The dedicated field wins for that section, so fallback extraction cannot replace more structured source data.

## Scope

### In

- R1: Preserve semantic line and list boundaries in Saramin detail fields.
- R2: Extract qualification and preference fallbacks from recognized decoded-body headings.
- R3: Apply fallback independently per section, only when its dedicated field is empty.
- R4: Prove the generated Markdown produces requirement manifest parents.

### Out

- Re-extracting or rewriting existing canonical records.
- Changing requirement-manifest parsing rules for other platforms.
- Splitting arbitrary inline hyphens into bullets.
- Adding new CLI flags, configuration, dependencies, or storage fields.

## Assumptions and Preconditions

| Claim | Command | Observed at | Observed result | Evidence source |
|---|---|---|---|---|
| Detail-field extraction currently removes all tags and collapses whitespace. | `sed -n '175,188p' src/careerkit/jobs/adapters/platforms/saramin.py` | `2026-08-01T17:23:31Z` | Tag boundaries become spaces, then all whitespace becomes one line. | Working tree on `fix/saramin-requirement-manifest` |
| Saramin automation currently uses decoded body text only as introduction content. | `sed -n '603,656p' src/careerkit/jobs/application/automation.py` | `2026-08-01T17:23:31Z` | Requirements and preferences read dedicated fields only; main duties are empty. | Working tree on `fix/saramin-requirement-manifest` |
| The manifest recognizes requirement items only when a supported bullet starts a line. | `sed -n '49p;147,210p' src/careerkit/jobs/application/requirement_manifest.py` | `2026-08-01T17:23:31Z` | `BULLET_RE` is line-anchored and prose-only qualification text does not create parents. | Working tree on `fix/saramin-requirement-manifest` |
| Automation tests have no Saramin-specific extraction case. | `rg -n "saramin\|Saramin" tests/jobs/application/test_automation.py` | `2026-08-01T17:23:31Z` | No matches. | Working tree on `fix/saramin-requirement-manifest` |

## Architecture

The Saramin adapter remains responsible for platform HTML interpretation. Detail-field extraction converts `<li>` elements into canonical `- ` items. It preserves `<br>` and paragraph boundaries as newlines. Break tags do not create bullets when the source has none.

Decoded-body extraction preserves the same break, paragraph, and list boundaries. The adapter then identifies normalized heading lines after an optional `■` or `(number)` prefix.

Qualification headings are `자격요건`, `자격 요건`, `지원자격`, `지원 자격`, `필수요건`, and `필수 요건`. Preference headings are `우대사항` and `우대 사항`.

The following non-target heading labels end an active section:

- Major duties: `주요업무`, `주요 업무`, `담당업무`, `담당 업무`, `업무내용`, `업무 내용`.
- Work conditions: `근무조건`, `근무 조건`, `근무환경`, `근무 환경`.
- Benefits: `복리후생`, `복리 후생`, `혜택`, `혜택 및 복지`.
- Hiring process: `전형절차`, `전형 절차`, `채용절차`, `채용 절차`.
- Company introduction: `회사소개`, `회사 소개`, `기업소개`, `기업 소개`.

Other lines remain section content. Each nonempty fallback content line becomes one canonical bullet. An empty or malformed body yields no fallback and no error.

Automation keeps the existing canonical Markdown formatter. For each affected section, it selects the dedicated detail field first and the parsed body section second. The requirement-manifest parser remains unchanged.

## Interface and Data Flow

No public interface changes. The internal flow is:

1. Fetch and decode the Saramin detail page.
2. Preserve detail-field line and list boundaries.
3. Identify relevant decoded-body sections.
4. Select structured field or per-section fallback.
5. Render canonical Markdown bullets.
6. Build the existing requirement manifest during screening.

## Testing

- Add adapter tests for `<br>`, paragraph, and list boundaries.
- Prove that only list elements or existing source markers create detail-field bullets.
- Add body-section tests for target headings and each non-target boundary group.
- Cover `■ 자격요건`, `(1) 자격 요건`, and preference variants.
- Add automation tests for field-first precedence and body-only fallback.
- Add an end-to-end synthetic detail-page test that renders Markdown and builds a manifest.
- Assert on manifest parents, not only intermediate extracted strings.
- Use synthetic fixtures without company names or platform record IDs.

## Risks

- **False section matches:** Match only the finite target and boundary labels above.
- **Section overrun:** Stop at the next recognized boundary. Add a test for each boundary group.
- **Duplicate content:** Apply fallback only when the dedicated field for that section is empty.
- **Regression in scalar fields:** Keep ordinary one-line values unchanged and cover them in adapter tests.

## Success Criteria

1. A qualification detail field with semantic HTML boundaries produces at least one required manifest parent.
   - **Measured by**: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/platforms/test_saramin.py tests/jobs/application/test_automation.py tests/jobs/application/test_requirement_manifest.py -q`
2. A body-only posting with a supported qualification heading produces required manifest parents.
   - **Measured by**: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/application/test_automation.py -q`
3. Dedicated qualification and preference fields override body fallbacks independently.
   - **Measured by**: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs/application/test_automation.py -q`
4. Existing jobs behavior remains green.
   - **Measured by**: `UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest tests/jobs -q`

## Open Decisions

None. Planning may choose private helper names, but it must preserve the source precedence and scope above.
