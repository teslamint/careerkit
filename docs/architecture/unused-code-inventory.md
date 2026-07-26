# Unused Code Inventory

## U7 Migrated-Package Gate

The blocking migrated-package command is:

```bash
UV_CACHE_DIR=.uv-cache uv run vulture src tests/static/vulture_whitelist.py --min-confidence 60
```

It reports zero unreviewed findings. The whitelist is symbol-specific; every line names a
protocol, dynamic dispatch, returned result shape, persisted schema member, or migration seam
with focused regression coverage. U7 also deleted the remaining package-only wrappers and
helpers that had no production caller, protocol obligation, or retained migration purpose.

| Retained finding | Reason and evidence |
|---|---|
| semantic encoder `inputs`, `normalize_embeddings` | Keyword names belong to the injected encoder protocol exercised by semantic-filter tests. |
| SQLite `row_factory` | `sqlite3` consumes the assigned attribute dynamically; index tests require name-addressable rows. |
| company result fields | `CompanyData` is returned through validation results; parser and CLI tests cover the result shape. |
| config preview fields | Preview results are serialized by the config CLI contract tests. |
| `StorageMigrator` | Internal copy-first migration seam retained and exercised by migration/cutover tests. |
| HTTP handler methods | `BaseHTTPRequestHandler` dispatches `do_*` and `log_message` by name; console tests exercise the server contract. |
| application statuses and `migration_source` | Persisted canonical schema members covered by model/storage round-trip tests. |

The inventory below records the earlier U12 legacy-tree review and remains as historical
evidence for the final U11 deletion decisions.

U12 reviewed inventory for Vulture findings across tracked executable Python. Evidence-backed dead symbols were deleted in this unit; remaining findings are either dynamic/protocol/test/public obligations or legacy code assigned to the final cutover.

## Commands

- `git ls-files '*.py'` → 175 tracked Python files reviewed.
- `UV_CACHE_DIR=$PWD/.tmp/uv-cache uv run vulture $(git ls-files '*.py') --min-confidence 100` → exit 3, 2 protocol-signature findings (expected for review mode; both dispositioned below).
- `UV_CACHE_DIR=$PWD/.tmp/uv-cache uv run vulture $(git ls-files '*.py') --min-confidence 60` → exit 3, 58 findings total (56 at 60%, 2 at 100%; all dispositioned below).
- Mapping audit helper (`python3`) → verified that every current Vulture finding is covered by one disposition row and that no blanket suppression is needed.

## Disposition Summary

- **deleted in U12**: 23 findings (19 original findings plus 4 follow-on constants exposed by deletion)
- **later migration deletion**: 15 current findings
- **retained dynamic/protocol/public/test obligation**: 43 current findings

## Finding Disposition

| Findings | Disposition | Evidence |
|---|---|---|
| `scripts/fix_company_info_format.py:159 function fix_avg_salary_nonstandard_table (60%)`<br>`templates/build/docx_helpers.py:79 function find_paragraph (60%)`<br>`templates/build/resume_builder.py:306 function build_company_short (60%)`<br>`templates/jd/audit_05.py:519 function process_screening_file (60%)` | later migration deletion | Legacy script/private helper with no tracked callers; the surrounding entrypoints are already earmarked in docs/architecture/entrypoint-inventory.md for removal at U11 after packaged replacements land. |
| `templates/jd/audit_hold_causes.py:46 variable VERDICT_POS (60%)`<br>`templates/jd/audit_hold_causes.py:143 variable no_screening (60%)`<br>`templates/jd/audit_hold_causes.py:147 variable no_screening (60%)`<br>`templates/jd/audit_hypotheses.py:55 variable H3_VERDICT_SCOPE (60%)`<br>`templates/jd/audit_hypotheses.py:101 function extract_id (60%)`<br>`templates/jd/audit_hypotheses.py:112 function load_company_slugs (60%)`<br>`templates/jd/audit_overlap.py:91 function extract_id (60%)` | later migration deletion | Dead constants/helpers inside legacy audit scripts. Those scripts are tracked in the entrypoint inventory as temporary migration-era tooling, so the unused internals can be removed when the scripts are collapsed during U11 rather than mixed into the structural refactor. |
| `templates/jd/jd_content.py:38 function load_company_info (60%)`<br>`templates/jd/jd_content.py:55 function generate_jd_filename (60%)`<br>`templates/jd/jd_content.py:66 function update_summary (60%)`<br>`templates/jd/path_utils.py:144 function find_jd_anywhere (60%)` | later migration deletion | Legacy compatibility helpers with no tracked callers in the canonical-storage flow. They are safe to remove when the remaining legacy module surface is retired, but were left untouched in this review-only slice to avoid mixing behavior changes with the migration. |
| `templates/jd/auto_company.py:217 function _existing_needs_thevc_enrichment (60%)` | deleted in U12 | No repo caller references `_existing_needs_thevc_enrichment`; the active code path calls `ensure_company_info*` directly and this helper is not part of any documented CLI/API contract. |
| `templates/jd/auto_processor.py:187 attribute classified_folder (60%)`<br>`templates/jd/auto_processor.py:238 attribute classified_folder (60%)`<br>`templates/jd/auto_state.py:118 variable classified_folder (60%)` | retained dynamic/protocol/public/test obligation | `AutoTaskResult.classified_folder` is part of the persisted auto-run result schema: `save_results()` serializes dataclass instances via `asdict(result)`, and `auto_processor` populates the field before the JSON artifact is written. |
| `templates/jd/auto_processor.py:188 attribute error_stage (60%)`<br>`templates/jd/auto_processor.py:211 attribute error_stage (60%)`<br>`templates/jd/auto_processor.py:247 attribute error_stage (60%)`<br>`templates/jd/auto_state.py:119 variable error_stage (60%)` | retained dynamic/protocol/public/test obligation | `AutoTaskResult.error_stage` is likewise part of the on-disk auto-run result schema. Vulture cannot see the JSON consumer path, but `save_results()` persists it and multiple failure branches populate it intentionally. |
| `templates/jd/auto_screening.py:330 function _run_llm (60%)` | deleted in U12 | No repo caller references `_run_llm`; screening code uses `CLIProvider().run(...)` through other paths, so this wrapper is currently inert. |
| `templates/jd/constants.py:16 variable JD_ANALYSIS_DIR (60%)` | deleted in U12 | `JD_ANALYSIS_DIR` has no remaining repo references after canonical storage moved summaries under `private/jd/derived/`. |
| `templates/jd/generate_index.py:24 variable jd_folder (60%)`<br>`templates/jd/generate_index.py:25 variable jd_filename (60%)`<br>`templates/jd/generate_index.py:26 variable screening_filename (60%)`<br>`templates/jd/generate_index.py:28 variable company_info_filename (60%)` | retained dynamic/protocol/public/test obligation | `CrossRef` is a compatibility helper object returned by `build_cross_reference()`. These dataclass fields are populated at construction time and form that helper's public data shape even though the current tests assert only a subset of fields. |
| `templates/jd/regenerate_summary.py:58 function _verdict_emoji (60%)` | deleted in U12 | No repo caller references `_verdict_emoji`, and `regenerate()` now formats verdict labels directly from `_VERDICT_LABEL`. |
| `templates/jd/search.py:315 function _scrape_with_playwright_or_fallback (60%)` | deleted in U12 | No repo caller references `_scrape_with_playwright_or_fallback`; current Wanted/Remember search paths call the API/prefetch helpers directly. |
| `templates/jd/search.py:498 variable total_duplicates (60%)`<br>`templates/jd/search.py:543 variable total_duplicates (60%)`<br>`templates/jd/search.py:568 variable total_duplicates (60%)`<br>`templates/jd/search.py:593 variable total_duplicates (60%)` | deleted in U12 | `total_duplicates` is incremented but never printed, returned, or persisted. The user-visible summary already reports `persisted_duplicates`, `run_duplicates`, and `filesystem_duplicates` directly. |
| `templates/jd/search.py:177 function _playwright_allowed (60%)`<br>`templates/jd/search.py:189 function _load_sync_playwright (60%)` | retained dynamic/protocol/public/test obligation | These compatibility seams are patched and asserted by `templates/tests/test_groupby_support.py` to prove seatbelt and browser-fallback behavior; deleting them breaks the characterization contract even though production calls are indirect. |
| `templates/jd/storage/index.py:215 attribute row_factory (60%)` | retained dynamic/protocol/public/test obligation | `connection.row_factory = sqlite3.Row` is required protocol setup for `_indexed_record(row)` to access columns by name (`row["platform"]`, etc.). Vulture misses this because the side effect is consumed by `sqlite3` internals. |
| `templates/jd/storage/migration.py:116 variable digest (60%)` | deleted in U12 | `_LegacyJD.digest` is populated when staging legacy records but never read by migration logic, reports, or tests. |
| `templates/jd/storage/migration.py:657 method _resolve_runtime_id (60%)` | deleted in U12 | No repo caller references `_resolve_runtime_id`; `_rewrite_runtime()` resolves runtime identities without calling this helper. |
| `templates/jd/web_console/server.py:81 method do_GET (60%)`<br>`templates/jd/web_console/server.py:99 method do_POST (60%)`<br>`templates/jd/web_console/server.py:102 method do_PUT (60%)`<br>`templates/jd/web_console/server.py:105 method do_DELETE (60%)`<br>`templates/jd/web_console/server.py:108 method do_PATCH (60%)`<br>`templates/jd/web_console/server.py:111 method log_message (60%)` | retained dynamic/protocol/public/test obligation | These methods are dynamic `BaseHTTPRequestHandler` protocol hooks (`do_*`, `log_message`) invoked by the stdlib HTTP server by name, not by repo-local callers. |
| `templates/tests/conftest.py:25 attribute __path__ (60%)`<br>`templates/tests/conftest.py:28 attribute submodule_search_locations (60%)`<br>`templates/tests/conftest.py:29 attribute __spec__ (60%)`<br>`templates/tests/conftest.py:82 method create_module (60%)`<br>`templates/tests/conftest.py:97 attribute __loader__ (60%)`<br>`templates/tests/conftest.py:98 attribute __spec__ (60%)`<br>`templates/tests/conftest.py:106 method find_spec (60%)` | retained dynamic/protocol/public/test obligation | These attrs/methods implement importlib and pytest test-loader protocol glue for legacy module aliasing. They are consumed indirectly by Python import machinery, not by explicit repo call sites. |
| `templates/tests/test_ce_saramin_http.py:8 function _no_sleep (60%)` | retained dynamic/protocol/public/test obligation | `@pytest.fixture(autouse=True)` registers `_no_sleep` by decoration; pytest injects it dynamically to patch `time.sleep` for every test in the module. |
| `templates/tests/test_groupby_support.py:21 variable exc_type (100%)`<br>`templates/tests/test_groupby_support.py:49 variable exc_type (100%)` | retained dynamic/protocol/public/test obligation | Both `exc_type` parameters belong to `__exit__` context-manager protocol methods on test doubles. The names are unused intentionally but the signature must stay compatible with `with` dispatch. |
| `templates/tests/test_http_client_base.py:14 attribute __enter__ (60%)`<br>`templates/tests/test_http_client_base.py:15 attribute __exit__ (60%)`<br>`templates/tests/test_http_client_base.py:28 attribute __enter__ (60%)`<br>`templates/tests/test_http_client_base.py:29 attribute __exit__ (60%)`<br>`templates/tests/test_remember_client.py:18 attribute __enter__ (60%)`<br>`templates/tests/test_remember_client.py:19 attribute __exit__ (60%)`<br>`templates/tests/test_remember_client.py:41 attribute __enter__ (60%)`<br>`templates/tests/test_remember_client.py:42 attribute __exit__ (60%)`<br>`templates/tests/test_wanted_client.py:20 attribute __enter__ (60%)`<br>`templates/tests/test_wanted_client.py:21 attribute __exit__ (60%)`<br>`templates/tests/test_wanted_client.py:43 attribute __enter__ (60%)`<br>`templates/tests/test_wanted_client.py:44 attribute __exit__ (60%)` | retained dynamic/protocol/public/test obligation | The mocked `__enter__`/`__exit__` methods are required because the production HTTP helpers use `urlopen(...)` as a context manager. Vulture cannot infer that the magic methods are consumed through `with`. |
| `templates/tests/test_saramin_enrichment.py:12 variable WANTED_BODY (60%)` | deleted in U12 | `WANTED_BODY` has no repo references; the module's tests only exercise `STUB_BODY` and `RICH_BODY` fixtures. |
| `templates/tests/test_screening_rules.py:408 variable stack_label (100%)` | deleted in U12 | `stack_label` is only a parametrization label; the test body uses `position` and `requirements`, and each `pytest.param(...)` already supplies an explicit `id=`. |
| `templates/tests/test_search_helpers.py:73 variable mock_filter_experience (100%)`<br>`templates/tests/test_search_helpers.py:73 variable mock_is_duplicate (100%)`<br>`templates/tests/test_search_helpers.py:92 variable mock_filter_experience (100%)`<br>`templates/tests/test_search_helpers.py:92 variable mock_is_duplicate (100%)`<br>`templates/tests/test_search_helpers.py:111 variable mock_filter_experience (100%)`<br>`templates/tests/test_search_helpers.py:111 variable mock_is_duplicate (100%)` | deleted in U12 | The patch decorators already replace `filter_experience` / `is_duplicate`; the injected mock objects are never asserted or configured in the test bodies. |
| `tests/resume/pdf_visual_equivalence.py:13 variable raster_dpi (60%)` | retained dynamic/protocol/public/test obligation | `PdfVisualThresholds.raster_dpi` is part of the public threshold object loaded from `docker/render-versions.env` and mirrored in the baseline manifest/tests; the comparison helper uses sibling fields, but the config field is still a supported contract. |

## Notes

- `deleted in U12` records symbols removed after call-site and focused regression checks; follow-on dead constants in `auto_company.py` and `regenerate_summary.py` were removed in the same pass.
- `later migration deletion` is reserved for dead code inside legacy scripts/compatibility helpers already scheduled for cutover or deletion in the repository architecture plan; removing them during the corresponding migration keeps structural churn together.
- `retained dynamic/protocol/public/test obligation` means the symbol is exercised indirectly by stdlib protocols, pytest/importlib discovery, dataclass serialization, or a compatibility/public data shape that Vulture cannot infer statically.
