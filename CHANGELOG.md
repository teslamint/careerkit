# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- `--resume` flag for auto.py: resume from last incomplete run with stage-based skip
- Experience range filtering via `search_config.yaml` (`filters.min_experience_upper`, `filters.max_experience`)
- Notification system (`notifications.py`): alerts when recommended postings found
- `--dry-run`, `--search-only`, `--no-classify`, `--stop-on-error` flags for auto.py

### Changed
- State persistence: atomic state file writes with `os.replace` and directory fsync
- JD pipeline refactored into modular extractors (`ce_wanted.py`, `ce_saramin.py`, `ce_thevc.py`, `ce_jd_files.py`)
- `DiscoveredJob` base class introduced for `JobPosting` and `QueueItem` (`models.py`)
- Search helpers extracted to `search_helpers.py` (shared page load + DOM scraping)
- JD content parsing extracted to `jd_content.py` (metadata, frontmatter, status)
- Search dedup logic consolidated in `search_helpers.py`
- `parse_remember_experience` moved to `jd_content.py`

### Fixed
- Resume logic with stage-based skip and protected status files
- Per-link exception isolation in search helpers
- `ensure_company_info` connected to Playwright-based extraction
- Investment section preserved for startups (enrich instead of replace)
