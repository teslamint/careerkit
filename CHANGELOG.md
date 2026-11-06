# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `--resume` flag for auto.py: resume from last incomplete run with stage-based skip
- Experience range filtering via `search_config.yaml` (`filters.min_experience_upper`, `filters.max_experience`)
- Notification system (`notifications.py`): alerts when recommended postings found
- `--dry-run`, `--search-only`, `--no-classify`, `--stop-on-error` flags for auto.py
- `career-resume verify-content` accepts any generated document (`--all-lines` checks claims outside blockquotes) and fails on `forbidden_terms` from the verifier config on every line (#28).

### Changed
- State persistence: atomic state file writes with `os.replace` and directory fsync
- JD pipeline refactored into modular extractors (`ce_wanted.py`, `ce_saramin.py`, `ce_thevc.py`, `ce_jd_files.py`)
- `DiscoveredJob` base class introduced for `JobPosting` and `QueueItem` (`models.py`)
- Search helpers extracted to `search_helpers.py` (shared page load + DOM scraping)
- JD content parsing extracted to `jd_content.py` (metadata, frontmatter, status)
- Search dedup logic consolidated in `search_helpers.py`
- `parse_remember_experience` moved to `jd_content.py`

### Removed
- Remove the unreachable semantic screening validator and its dead gate tests (#35).

### Fixed
- Saramin detail cells route the bracketed section labels inside one cell, so a collapsed 자격요건 cell no longer files 우대 items under 자격 요건 or stores "상세보기"/"더보기" chrome as requirements (#63).
- A demoted screening row drops the citation the evidence check rejected instead of keeping it beside `없음` (#63).
- A requirement written with a category noun such as "Application 개발 경력" keeps its 충족 claim instead of being demoted on a word the résumé never needs to write (#63).
- Continue the screening provider chain after contract-invalid output and retry once with quality issue codes (#34, #36); `probe_screening_provider_contract` may now report quality retries as retried.
- `career-jobs record show` reports `screening_provider`, and the derived screening summary marks fallback verdicts with `(fallback)` (#27).

- Search query normalization keeps stack terms (`PHP Laravel 백엔드` is sent verbatim), widens only to a bare role word, and reports widened queries in the run diagnostics (#26).
- Reject malformed posting URLs without stopping extraction of the remaining batch or browser-origin candidates.
- Resolve legacy stored postings only from their own posting path, not from addresses embedded in query strings.
- Reject empty or malformed GroupBy posting data during extraction and report its status as unknown.
- Summary rebuild falls back to the record creation date when a legacy source filename is too long.
- Resume logic with stage-based skip and protected status files
- Per-link exception isolation in search helpers
- `ensure_company_info` connected to Playwright-based extraction
- Investment section preserved for startups (enrich instead of replace)
