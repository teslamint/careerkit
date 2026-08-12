# AI Agent Project Guide

Resume builder & job search automation system. Markdown source → PDF/HTML/TXT output.

> **Full documentation**: [Getting Started](docs/getting-started.md) · [Customization](docs/customization.md) · [AI Workflow](docs/ai-workflow.md)

Reusable implementation guidance and solved-problem notes live in
`docs/solutions/`, and `CONCEPTS.md` holds this repository's canonical
vocabulary — both are worth checking before working in a documented area.

## Project Overview

Source files in `private/profile/` and `private/companies/` → `careerkit.resume` → output in `private/build/` (gitignored).
Job search automation lives in `careerkit.jobs`, with company info (`private/company_info/`), canonical JD records (`private/jd/records/`), runtime state (`private/jd/runtime/`), and rebuildable views (`private/jd/derived/`). See [ARCHITECTURE.md](ARCHITECTURE.md) for ownership and change points.

## Repository Boundary

Code development happens in the public `careerkit` repository. The private
workspace repository holds the resume sources, the job-search data under
`private/`, and the lifecycle record (`docs/plans/`, `docs/retros/`,
`docs/specs/`, `docs/deviations/`, `docs/research/`, `docs/superpowers/`,
`ROADMAP.md`, `LESSONS.md`, `.release-loop/`). Neither side commits the other's
material: a code change belongs in `careerkit`, and a plan, retro, or screening
outcome belongs in the workspace repository.

## Critical Rules

- **Markdown is source of truth** — edit `private/profile/` and `private/companies/`, never `private/build/` outputs
- **Conventional Commits** with scope: `docs(company): add portfolio`, `fix(builder): handle edge case`
- **Variant tags** filter content per resume type (`public` vs `job`)
- **Do not embellish resume content** — only claim technologies/patterns evidenced in the codebase

## Critical Gotchas

### 1. Variant Tag Syntax
```html
<!-- public-only:start --> / <!-- public-only:end -->   ✅ CORRECT
<!-- job-only:start -->    / <!-- job-only:end -->       ✅ CORRECT
<!-- variant:public -->    / <!-- /variant:public -->    ❌ WRONG (not filtered, causes duplicates)
```
When fixing variant tags, convert ALL four tags (opening/closing for both variants).

### 2. Summary Mode Only Reads `## Overview`
`extract_overview()` reads content between `## Overview` and next `## `. Content in `## Summary` or later sections is **ignored**. Place summary-mode content inside `## Overview` with `job-only` tags.

### 3. Full-Mode Override Requires ALL Project Files
Override is file-level. Full-mode companies need ALL `private/companies/<company>/projects/` files overridden. Missing → original (possibly Korean) content leaks.

### 4. Company Key Case Sensitivity
`config.json` keys must match directory names exactly: `"CompanyB"` not `"companyb"`.

### 5. variant_config.json is Gitignored
Must create manually: `cp variant_config.example.json private/variant_config.json`

## Guard and Contract Code

Rules distilled from defects in the `careerkit.jobs` publication guards. Every one of
these shipped past a green test suite and was caught only in review, so treat them as
checks to run while writing, not afterwards.

- **Guards allowlist, never denylist.** A check deciding whether model output may be
  published enumerates what is permitted. A denylist admits every label nobody thought
  of: the `fallback` provider passed a local-provider denylist and cleared a cap it had
  never lifted.
- **Reject before any fallback branch.** When a guard rejects a candidate, return it as
  rejected immediately. A lookup below that re-admits it makes the guard decorative — a
  workspace-containment check was undone by a basename fallback three lines later.
- **Fixed-shape contracts check exact counts.** `!= 4`, not `< 4`. A row with a fifth
  column parsed cleanly and hid a citation past the last cell anything read.
- **Change the writer with the reader.** A format this code both parses and rewrites gets
  both sides changed together, plus a round-trip test. Fixing only the parser leaves the
  writer editing the wrong cell — strictly worse than the bug being fixed.
- **New validation covers every entry point, including this application's own output.**
  A rule added inside a service path must be checked against the standalone CLI and
  against documents the code itself generates. `screening validate` began rejecting the
  fallback document `run_screening` had just written, because the service path never
  reaches that branch.
- **Derived values follow the mutation they describe.** A metric reading a document's
  final state is computed after the document is final; a metric counting what the model
  wrote stays before. Say which one it is in a comment — the next reader will otherwise
  "fix" the correct half.
- **Batch aggregation sums.** `dict.update` on per-item telemetry keeps only the last
  item, hiding exactly the failures the telemetry exists to surface. Use `Counter`, or
  count per key. (`Counter.update` sums; plain `dict.update` replaces.)
- **Re-read the authoritative field, never a proxy.** Recovery tallies read the persisted
  flag, not the outcome of a downstream call that can skip for unrelated reasons.
- **Numeric CLI options that slice or index use `_positive_int`.** `--limit -1` sliced
  the whole queue and would have re-published nearly every record.
- **A test must not neutralize what it tests.** A double supplying an empty allowlist, or
  a fake whose shape differs from the real result, hides the bypass being asserted.
- **Changing a tuned threshold's inputs requires re-measuring it.** R5/R6 thresholds come
  from a corpus run. Any change to what counts as a match re-runs that measurement before
  it lands.

Job-search data never becomes a tracked artifact: platform job IDs, company and team
names, employment types, and screening outcomes stay under `private/`. A `.gitignore`
rule does not untrack what is already committed (`git rm -r --cached` does), and a public
remote keeps whatever was already pushed.

## Resume Content Integrity

Override content must not add technologies, roles, or achievements absent from base `private/companies/` or `private/profile/` files.

**Inflation patterns to reject:**
- **Role**: adding 매니저/리드/총괄 when actual role is IC
- **Verb**: 재설계 when actual work was 분리; 전환 when actual was 설계
- **Scope**: Cluster when infra was managed services; Kubernetes when actual was ECS
- **Architecture**: MSA 전환 when actual was partial service extraction

### Generated Content Integrity

AI-generated content (interview sheets, mock interviews) must not infer specific technical experiences from general resume statements. "Used Spring Boot" ≠ "solved JPA N+1". State only what the resume explicitly documents. Verify with: `uv run career-resume verify-content private/jd_analysis/interview/<file>.md`

## Review Guidelines

When reviewing pull requests, inspect the diff first, then read surrounding files only as needed to verify behavior. Keep the review scoped to changes introduced by the pull request.

Prioritize correctness, regressions, missing tests, and repository-specific rule violations over general advice. Report actionable findings first, ordered by severity, with concrete file/line references when possible. Do not suggest broad refactors unless they prevent a concrete bug or regression.

Review focus:
- **Resume source of truth**: Markdown source files are authoritative. Do not recommend editing generated files under `private/build/`.
- **Content integrity**: Reject changes that add technologies, roles, scope, or achievements not evidenced by `private/profile/` or `private/companies/`.
- **Builder behavior**: Check variant tag handling, summary extraction behavior, override behavior, company key case sensitivity, and example build compatibility.
- **JD pipeline behavior**: Check classification paths, validation behavior, false positives, Korean screening wording constraints, duplicate detection, and queue/status handling.
- **Generated analysis content**: Check that prompts and validators prevent unsupported inference from general resume statements.
- **Workflow automation**: Check secret exposure, fork PR behavior, permissions, and whether generated comments can spam a PR.

If there are no actionable findings, say that clearly and mention any residual verification gap.

## Build & Test Commands

```bash
# Resume builds
uv run career-resume build <public|job|example> [full|short|wanted|career|packet|base|all] [--target <name>] [--clean]

# Quick validation
uv run career-resume build public all && uv run career-resume build job all

# Unit tests
uv run pytest tests/jobs/test_status.py -v
```

## Plan Review in Codex

When creating a non-trivial coding plan in Codex, ask whether the user wants a sub-agent review unless they have already explicitly requested it.

If the user explicitly requests sub-agent/advisor-style review, spawn one sub-agent to review the draft plan before presenting the final plan.

The sub-agent review should:
- Check whether the plan matches repository constraints and current code reality
- Identify missing tests, risky assumptions, and unintended side effects
- Avoid implementing changes
- Return concise actionable feedback

If sub-agents are unavailable or not explicitly approved, state that the advisor/sub-agent review was skipped and proceed with the best local review.

## Key Directory Structure

```
private/                → all personal data (gitignored)
  profile/              → core sections (contact, summary, skills, education)
  companies/<co>/       → per-company: profile.md, projects/, achievements/
  overrides/<target>/   → target-specific overrides (mirror profile/ & companies/)
  build/                → generated outputs
  company_info/         → company database
  jd/records/           → canonical JD and screening revisions
  jd/runtime/           → queues and run state
  jd/derived/           → rebuildable index and summary
src/careerkit/resume/   → resume product
src/careerkit/jobs/     → job-search product and platform adapters
```

## File Naming

| Directory | Pattern | Example |
|-----------|---------|---------|
| `private/company_info/` | `<company>.md` | `techcorp.md` |
| `private/jd/records/` | `<platform>/<job-id>/record.json` | `wanted/123456/record.json` |
| record content revision | `<platform>/<job-id>/content/<revision>/{jd,screening}.md` | managed by `JDRecordRepository` |
