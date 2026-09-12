# pyright: reportUndefinedVariable=false, reportUnusedExpression=false

# Semantic encoder protocol parameters are part of the third-party callable contract.
inputs
normalize_embeddings

# sqlite3 consumes this attribute dynamically when rows are materialized.
_.row_factory

# CompanyData is returned through CompanyValidationSummary as a public result shape.
_.name_en
_.industry
_.avg_salary
_.revenue

# Config preview fields are serialized by the CLI response adapter.
_.converted_config
_.would_write

# StorageMigrator is an intentionally internal maintenance seam covered by migration tests.
StorageMigrator

# BaseHTTPRequestHandler dispatches these methods by name.
_.do_GET
_.do_POST
_.do_PUT
_.do_DELETE
_.do_PATCH
_.log_message

# urllib invokes redirect handlers dynamically through the opener chain.
_.redirect_request

# RememberCompanyInfo dataclass fields are consumed by CLI JSON serialization.
_.established
_.employee_count
_.avg_salary_manwon
_.salary_yoy_change
_.employee_stats
_.company_type
_.homepage
_.ceo
_.tags

# Persisted enum/schema members remain part of the canonical record contract.
_.INTERVIEW
_.OFFER
_.migration_source

# Saramin company info functions are public API consumed by tests and CLI.
extract_csn_from_html
saramin_company_http
format_company_markdown

# Semantic-eval public and tested contracts are exercised through CLI and unit tests.
_.queue_case_count
_.incomplete_error_code
build_gold_dataset

# HTMLParser dispatches this callback by name.
_.handle_data

# TheVCCompanyInfo dataclass fields are consumed by CLI JSON serialization.
_.last_funded_on

# Wanted and GroupBy company discovery utilities are reserved for the approved follow-up.
wanted_search_company_id
groupby_company_from_position

# Portfolio-research contracts are consumed by private operations and later approved units.
_integer
required_eval_gate
calibrate_human_rubric
grade_portfolio_eval_output
compare_eval_results
eval_activation_allowed
render_manifest
render_catalog
render_evidence
load_evidence
render_proposal
load_proposal
prepare_proposal
apply_proposal
_.__cause__
cleanup_stale_dispositions
write_sanitized_t1_evidence
_.allows_transition_to
_.completed_targets
EvalCase
EvalGate
GoldClaim
Judgment
CalibrationResult
ComprehensiveEvalResult
_.input_token_regression
_.output_token_regression
_.p95_latency_regression
