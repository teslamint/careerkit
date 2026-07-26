# Legacy Behavior Parity Audit

Systematic post-cutover audit of every legacy product entrypoint in the U0 inventory.
The last commit containing the legacy tree is `132a340^`; the suggested
`7da1e46^` reference is already post-cutover and contains no `templates/` tree.

## Method and scope

- Audited all 65 U0 executable rows: 45 legacy product entrypoints below, 18
  test convenience mains through `test-inventory.md`, and the two retained
  `careerkit` composition roots through CLI installation tests.
- For each product row, inspected configuration reads, material branches,
  filters, operator-visible outputs, and persistent state writes.
- Outcomes are `parity`, an equivalent invariant/no-op, a documented replacement,
  or a recorded removal backed by the U0 caller disposition. No row is unresolved.
- Regression repairs produced by this sweep cover search state controls and
  overrides, closure backfill, stale-screening CSV output, and all operator-visible
  Remember fields. Earlier bot rounds already repaired schema, rendering, index,
  search request/result artifacts, platform defaults, pagination metadata, and
  failure/resume behavior.
- The first post-audit review pass additionally locked eight edge branches:
  company-info slug symlinks, target-neutral base builds, default candidate
  context, default employment labels, inter-request delay, Remember 0/0
  experience, legacy bullet/frontmatter metadata, and GroupBy detail experience.
- The second post-audit pass locked missing-rule fallback, computed company-risk
  summaries, ignored example artifacts, GroupBy max-only bounds, application
  status timestamps, canonical-duplicate state writes, and accurate historical
  migration guidance.
- The third post-audit pass locked automation max-url overrides, partial Wanted
  pagination, existing-record extraction deduplication, and the pre-screen
  company-info completeness gate.
- The fourth post-audit pass locked dry-run classification, canonical screening
  output paths, controlled target errors, and workspace-relative URL inputs.
- The fifth post-audit pass locked first-run config errors, variant-safe education,
  legacy project periods, deferred screening config reads, explicit platform disablement,
  positive limits, and controlled screening failures.
- The sixth post-audit pass locked partial platform pages, extraction-only dry-runs,
  migrated resume state, and real queue rescreening.
- The seventh post-audit pass locked verifier JSON/errors, malformed config diagnostics,
  evidence-complete rescreen previews, renderer errors, and backup collisions.
- The eighth post-audit pass locked forced screening-only runs, latest incomplete
  migrated state recovery, and nullable optional config diagnostics.
- The ninth post-audit pass locked compound JSON job keys and duplicate-safe
  cumulative search counts.

## Per-entrypoint disposition

| Legacy entrypoint | Successor | Public surface | Config / branch / filter / output / state disposition | Result |
|---|---|---|---|---|
| `example/interview/build-sheet.py` | `deleted` | `none` | No tracked or external recurring caller; private device workflow is independent | `delete-with-evidence` |
| `main.py` | `deleted` | `none` | Placeholder only; no behavior | `delete-with-evidence` |
| `scripts/audit_company_info.py` | `deleted` | `company validate` | Historical audit only; no active caller | `delete-with-evidence` |
| `scripts/fix_company_info_format.py` | `deleted` | `company validate --fix` | One-time repair; normalized validation/fix retained | `delete-with-evidence` |
| `scripts/migrate_company_slugs.py` | `deleted` | `domain naming` | One-time slug migration; normalization rule retained | `delete-with-evidence` |
| `templates/build/career_builder.py` | `career-description.py + resume CLI` | `build career/packet` | Company ordering, project sections, md/pdf separator branches and render outputs retained | `parity` |
| `templates/build/generate_notes.py` | `notes.py + resume CLI` | `notes and build orchestration` | Diff counts, clean mode and notes output retained | `parity` |
| `templates/build/headhunter_filler.py` | `headhunter.py + resume CLI` | `headhunter analyze only` | Template analysis retained; fill/data and private mapping keys explicitly removed because U0 proved no recurring operator caller | `recorded-removal` |
| `templates/build/resume_builder.py` | `build.py + filesystem.py + resume CLI` | `build` | Variant/target merge, override resolution, full/short/wanted/pdf branches and artifact outputs retained | `parity` |
| `templates/build/schema.py` | `domain/schema.py + resume CLI` | `validate` | Contact/profile/project schemas and variant tags retained | `parity` |
| `templates/build/verify_content.py` | `verify_content.py + resume CLI` | `verify-content` | Evidence matching, finding output and exit status retained | `parity` |
| `templates/jd/audit_05.py` | `deleted` | `none` | Point-in-time analysis; no active caller or state write | `delete-with-evidence` |
| `templates/jd/audit_hold_causes.py` | `deleted` | `none` | Point-in-time analysis; no active caller or state write | `delete-with-evidence` |
| `templates/jd/audit_hypotheses.py` | `deleted` | `none` | Point-in-time analysis; no active caller or state write | `delete-with-evidence` |
| `templates/jd/audit_overlap.py` | `deleted` | `none` | Point-in-time analysis; only historical sampler caller | `delete-with-evidence` |
| `templates/jd/audit_sampler.py` | `deleted` | `none` | Point-in-time analysis; no active caller | `delete-with-evidence` |
| `templates/jd/auto.py` | `application/automation.py + jobs CLI` | `run auto` | Search/extract/prescreen/screen/classify/resume/results retained; external company enrichment, completeness gate and notification flags recorded as removed non-contract controls | `recorded-removal` |
| `templates/jd/backfill_closed_jds.py` | `maintenance.py + jobs CLI` | `storage backfill-closed` | All closure markers, dry-run/apply branch and compound-key output retained | `parity` |
| `templates/jd/backfill_wanted_company_info.py` | `deleted` | `none` | One-time enrichment repair; no active caller | `delete-with-evidence` |
| `templates/jd/cleanup_unprocessed.py` | `canonical storage` | `none` | Legacy classifier was already a no-op under canonical storage | `equivalent-no-op` |
| `templates/jd/company_extractor.py` | `automation.py + company_info.py` | `run auto/company validate` | Reusable JD/company parsing retained; standalone extractor had no active caller | `recorded-removal` |
| `templates/jd/company_validator.py` | `company_info.py + jobs CLI` | `company validate` | Parse, risk, completeness, fix/report branches retained | `parity` |
| `templates/jd/dedup_company_info.py` | `deleted` | `none` | One-time repair; no active caller, canonical file naming prevents duplicate keys | `delete-with-evidence` |
| `templates/jd/dedup_screening.py` | `file_records.py + preflight.py` | `storage preflight` | Canonical compound keys make duplicate screenings impossible; integrity errors retained | `equivalent-invariant` |
| `templates/jd/enrich_company_fields.py` | `deleted` | `none` | One-time repair; no active caller | `delete-with-evidence` |
| `templates/jd/enrich_saramin_company_info.py` | `deleted` | `none` | One-time enrichment; no active caller | `delete-with-evidence` |
| `templates/jd/enrich_thevc_company_info.py` | `deleted` | `none` | One-time enrichment; no active caller | `delete-with-evidence` |
| `templates/jd/freshness_check.py` | `maintenance.py + jobs CLI` | `summary stale-screenings` | days threshold, manifest-age filter, verdict column, sorted CSV output retained | `parity` |
| `templates/jd/generate_index.py` | `sqlite_index.py + maintenance.py + jobs CLI` | `index rebuild` | Atomic rebuild, integrity errors and canonical search.sqlite3 output retained | `parity` |
| `templates/jd/migrate_storage.py` | `storage_migration.py` | `internal one-time migration` | Preview/apply/verify/rollback behavior retained internally; public entrypoint removed after operator cutover | `recorded-removal` |
| `templates/jd/pipeline.py` | `pipeline.py + jobs CLI` | `ingest/record/queue` | Status axes, classification, rescreen and storage counts retained; monolithic CLI removed | `parity` |
| `templates/jd/quick_filter.py` | `title_filter.py + search.py` | `search/run auto` | Include/exclude/prefer filters retained; legacy batch JSON/queue files replaced by direct typed handoff | `recorded-replacement` |
| `templates/jd/recollect_company_info.py` | `deleted` | `none` | One-time recollection utility; no active caller | `delete-with-evidence` |
| `templates/jd/regenerate_summary.py` | `maintenance.py + jobs CLI` | `summary rebuild` | Verdict/status rows and derived Markdown output retained | `parity` |
| `templates/jd/remember_batch_extract.py` | `automation.py` | `run auto --from-urls` | URL identity, API payload, experience/salary/address, visible metadata, JD and result artifacts retained | `parity` |
| `templates/jd/rescreen_truncated.py` | `pipeline.py + jobs CLI` | `queue rescreen` | Reusable single-record rescreen retained; hard-coded historical ID list removed | `recorded-removal` |
| `templates/jd/screening_lint.py` | `screening_lint.py + jobs CLI` | `screening lint` | Policy findings, hook input, compound keys and exit status retained | `parity` |
| `templates/jd/search.py` | `search.py + platform adapters + maintenance.py + jobs CLI` | `search run/status/reset-state` | Config/platform/filter/cap/state/request-output behavior retained; browser fallback removed after exhaustive API adapters | `parity` |
| `templates/jd/search_quick.py` | `search.py + platform adapters` | `search run` | Full/quick implementations unified; browser scroll knobs and quick queue JSON replaced by exhaustive API/direct handoff | `recorded-replacement` |
| `templates/jd/semantic_poc.py` | `semantic_filter.py` | `search semantic filter` | Runtime model/threshold/classification retained; evaluation-only precision/recall CLI removed | `recorded-removal` |
| `templates/jd/wanted_extract.py` | `automation.py` | `run auto --from-urls` | Wanted identity/payload/metadata/JD persistence retained; standalone result print removed | `parity` |
| `templates/jd/web_console/server.py` | `console/server.py + static assets + jobs CLI` | `console serve` | Loopback server, search/filter/detail APIs, CSP and static UI retained | `parity` |
| `templates/jd/worker.py` | `automation.py + pipeline.py` | `run auto/queue` | Extraction/screen/classify and resumable failures retained; browser timeout knobs and legacy queue cleanup replaced | `recorded-replacement` |
| `build.sh` | `resume CLI` | `career-resume build` | All public variants/formats/target/clean orchestration retained | `parity` |
| `scripts/screen-jds.sh` | `automation.py + jobs CLI` | `career-jobs run auto --screening-only` | Screening-only batch flow retained without shell wrapper | `parity` |

## Explicit behavior differences

### Recorded removals

- **HeadHunter fill/data:** the analyzer is reusable and retained. Private mapping
  keys, template filling, salary/personal fields, and document writes were not
  retained because U0 found no recurring executable caller. Restoring them now
  requires a new product decision rather than silent compatibility.
- **Auto company enrichment and notifications:** the typed pipeline retains local
  company-file matching and completeness blocking for screening, but no longer
  performs external company recollection or result notifications. These were
  internal branches of the legacy auto flow but had no checked-in machine
  consumer. They are recorded here instead of being mistaken for parity.
- **One-time repair/audit commands:** hard-coded rescreen IDs, historical audits,
  slug/dedupe/enrichment repairs, and the completed storage activation command
  were deleted after their U0 caller checks. Reusable validation, naming,
  migration, integrity, and rescreen rules remain in package services.

### Replacements with stronger canonical contracts

- Legacy quick-search queue files and browser scroll controls are replaced by
  exhaustive platform API adapters plus direct typed URL handoff. Search request
  and result artifacts remain under canonical runtime directories.
- Duplicate screening cleanup is replaced by the `(platform, job_id)` repository
  invariant and integrity preflight. Legacy unprocessed-folder cleanup was already
  a no-op after canonical storage.
- Worker queue cleanup is replaced by resumable pending-URL state that retains
  only extraction and screening failures.

## Regression evidence

- `tests/jobs/test_cli.py`: search query/max/dry-run/status/reset controls and
  public maintenance commands.
- `tests/jobs/application/test_maintenance.py`: state metrics, corrupt-state
  recovery, atomic replacement, closure dry-run/apply, and sorted stale-screening
  output.
- `tests/jobs/application/test_automation.py`: complete Remember visible metadata
  and GroupBy detail metadata, plus search-only request handoff.
- `tests/jobs/application/test_company_info.py`, `tests/jobs/test_cli.py`, and
  `tests/resume/test_cli_and_resources.py`: safe alias resolution, manual-screening
  context, target-neutral base builds, and employment defaults.
- `tests/jobs/test_search_service.py`, `tests/jobs/platforms/test_platform_adapters.py`,
  and `tests/jobs/storage/test_migration.py`: inter-request delay, Remember 0/0
  experience, canonical duplicate state, GroupBy max-only bounds, and legacy
  metadata forms.
- `tests/jobs/application/test_screening.py`: absent-rule fallback and validator-
  computed company risk summaries.
- `tests/architecture/test_legacy_parity_audit.py`: U0-to-audit row completeness
  and allowed terminal dispositions.
