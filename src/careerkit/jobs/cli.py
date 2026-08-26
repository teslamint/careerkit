from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from careerkit.jobs.adapters.http import HttpClient
from careerkit.jobs.adapters.status_probes import SUPPORTED_PLATFORMS
from careerkit.jobs.application import screening_lint
from careerkit.jobs.application.automation import (
    AutomationRunResult,
    AutomationService,
    JobsCompletionStage,
    JobsExtractionStage,
    JobsAutoResultService,
    JobsResumeStateService,
    JobsScreeningStage,
    load_candidate_context,
)
from careerkit.cli_logging import configure_cli_logging
from careerkit.jobs.application.maintenance import CheckClosedResult, JobsMaintenanceService
from careerkit.jobs.application.company_info import CompanyInfoService
from careerkit.jobs.application.pipeline import IngestResult, JobsPipelineService, PrescreenedListing
from careerkit.jobs.adapters.screening.cli_provider import resolve_commands
from careerkit.jobs.application.screening import STRONG_PROVIDER_LABELS, is_fallback_document, run_screening, validate_screening_structure
from careerkit.jobs.application.storage_migration import get_platform_from_url
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.adapters.storage.link_store import LinkStore
from careerkit.jobs.application.linking import LinkService
from careerkit.jobs.console.server import create_server
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, PostingStatus, ScreeningVerdict
from careerkit.workspace import WorkspacePaths, WorkspaceResolutionError, resolve_workspace


class MaintenanceOps(Protocol):
    derived_dir: Path

    def relative_path(self, path: Path) -> str: ...
    def config_check(self) -> Any: ...
    def config_preview(self) -> Any: ...
    def config_apply(self) -> Any: ...
    def company_validate(self, *, file_name: str | None = None, fix: bool = False) -> Any: ...
    def search(self, *, queries: list[str] | None = None, max_urls: int | None = None) -> Any: ...
    def persist_seen_job_keys(
        self, seen_job_keys: set[str], *, new_count: int | None = None
    ) -> None: ...
    def search_status(self) -> Any: ...
    def reset_search_state(self) -> bool: ...
    def storage_preflight(self) -> Any: ...
    def cleanup_preflight(self, output_root: Path) -> None: ...
    def rebuild_index(self, *, database_path: Path | None = None) -> Any: ...
    def rebuild_summary(self, *, output_path: Path | None = None) -> Any: ...
    def backfill_closed(self, *, dry_run: bool = True) -> Any: ...
    def check_closed(
        self,
        *,
        dry_run: bool = True,
        delay: float = 1.0,
        platforms: tuple[str, ...] | None = None,
        recheck: bool = False,
    ) -> Any: ...
    def write_stale_screening_report(
        self, *, days: int = 30, output_path: Path | None = None
    ) -> Any: ...
    def semantic_eval_capture(self, *, output_path: Path, seed: int | None = None) -> Any: ...
    def semantic_eval_run(self, *, dataset_path: Path, output_path: Path) -> Any: ...
    def semantic_eval_compare(self, *, dataset_path: Path, incumbent_path: Path, candidate_path: Path, output_path: Path | None = None) -> Any: ...


class PipelineOps(Protocol):
    repository: JDRecordRepository

    def ingest_url(self, url: str) -> IngestResult: ...
    def ingest_file(self, path: Path) -> list[IngestResult]: ...
    def show_record(self, key: JobKey) -> Any: ...
    def set_record_status(
        self,
        key: JobKey,
        *,
        application_status: ApplicationStatus | None = None,
        posting_status: PostingStatus | None = None,
        application_status_updated_at: str | None = None,
        application_note: str | None = None,
    ) -> Any: ...
    def set_record_verdict(self, key: JobKey, verdict: ScreeningVerdict) -> Any: ...
    def queue_status(self) -> Any: ...
    def migrate_queue_status(self) -> list[dict[str, str]]: ...
    def classify_record(self, key: JobKey, *, dry_run: bool = False) -> IngestResult: ...
    def list_prescreened(self, *, reason: str | None = None) -> PrescreenedListing: ...
    def storage_status(self) -> dict[str, int]: ...


class AutomationOps(Protocol):
    def run(self, operation: str, args: list[str]) -> AutomationRunResult: ...


@dataclass(frozen=True)
class ServiceBundle:
    maintenance: MaintenanceOps
    pipeline: PipelineOps
    automation: AutomationOps
    link_service: LinkService | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="career-jobs")
    parser.add_argument("--workspace", help="Workspace root override")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity (default INFO, -v DEBUG)")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Run retained jobs automation")
    run.set_defaults(handler=_handle_missing_operation)
    run_subparsers = run.add_subparsers(dest="run_command")
    run_auto = run_subparsers.add_parser("auto", help="Run packaged jobs automation")
    run_auto.add_argument("--from-urls", type=Path)
    run_auto.add_argument("--search-only", action="store_true")
    run_auto.add_argument("--screening-only", action="store_true")
    run_auto.add_argument("--dry-run", action="store_true")
    run_auto.add_argument("--resume", action="store_true")
    run_auto.add_argument("--max-urls", type=int)
    run_auto.add_argument("--llm-timeout", type=int, default=120)
    run_auto.add_argument("--local-llm-timeout", type=int)
    run_auto.add_argument("--no-classify", action="store_true")
    run_auto.add_argument("--json", action="store_true")
    run_auto.set_defaults(handler=_handle_run_auto)

    config = subparsers.add_parser("config", help="Inspect or migrate jobs config")
    config.set_defaults(handler=_handle_missing_operation)
    config_subparsers = config.add_subparsers(dest="config_command")
    config_check = config_subparsers.add_parser("check", help="Read-only configuration readiness check")
    config_check.add_argument("--json", action="store_true")
    config_check.set_defaults(handler=_handle_config_check)
    config_preview = config_subparsers.add_parser("preview", help="Read-only configuration migration preview")
    config_preview.add_argument("--json", action="store_true")
    config_preview.set_defaults(handler=_handle_config_preview)
    config_apply = config_subparsers.add_parser("apply", help="Apply safe configuration migration")
    config_apply.add_argument("--json", action="store_true")
    config_apply.set_defaults(handler=_handle_config_apply)

    company = subparsers.add_parser("company", help="Manage company info records")
    company.set_defaults(handler=_handle_missing_operation)
    company_subparsers = company.add_subparsers(dest="company_command")
    company_validate = company_subparsers.add_parser("validate", help="Validate company info markdown")
    company_validate.add_argument("--file")
    company_validate.add_argument("--fix", action="store_true")
    company_validate.set_defaults(handler=_handle_company_validate)
    company_apply = company_subparsers.add_parser("apply", help="Apply validated company info markdown")
    company_apply.add_argument("--company-name", required=True)
    company_apply.add_argument("--input", type=Path, required=True)
    company_apply.add_argument("--expected-digest")
    company_apply.add_argument("--timeout", type=float, default=1.0)
    company_apply.add_argument("--json", action="store_true")
    company_apply.set_defaults(handler=_handle_company_apply)
    company_fetch = company_subparsers.add_parser("fetch", help="Fetch company info from platform API")
    company_fetch.add_argument("--platform", required=True, choices=["remember", "saramin", "wanted"])
    company_fetch.add_argument("--id", required=True, dest="company_id")
    company_fetch.add_argument("--json", action="store_true")
    company_fetch.set_defaults(handler=_handle_company_fetch)

    search = subparsers.add_parser("search", help="Search job postings with packaged adapters")
    search.set_defaults(handler=_handle_missing_operation)
    search_subparsers = search.add_subparsers(dest="search_command")
    search_run = search_subparsers.add_parser("run", help="Run configured job search")
    search_run.add_argument("--query", "-q", action="append")
    search_run.add_argument("--max-urls", type=_positive_int)
    search_run.add_argument("--dry-run", action="store_true")
    search_run.add_argument("--json", action="store_true")
    search_run.set_defaults(handler=_handle_search_run)
    search_status = search_subparsers.add_parser("status", help="Show persistent search state")
    search_status.add_argument("--json", action="store_true")
    search_status.set_defaults(handler=_handle_search_status)
    search_reset = search_subparsers.add_parser("reset-state", help="Reset persistent search state")
    search_reset.add_argument("--json", action="store_true")
    search_reset.set_defaults(handler=_handle_search_reset)

    ingest = subparsers.add_parser("ingest", help="Check URLs or files before extraction")
    ingest.set_defaults(handler=_handle_missing_operation)
    ingest_subparsers = ingest.add_subparsers(dest="ingest_command")
    ingest_url = ingest_subparsers.add_parser("url", help="Check one URL")
    ingest_url.add_argument("url")
    ingest_url.add_argument("--json", action="store_true")
    ingest_url.set_defaults(handler=_handle_ingest_url)
    ingest_file = ingest_subparsers.add_parser("file", help="Check a file of URLs")
    ingest_file.add_argument("path", type=Path)
    ingest_file.add_argument("--json", action="store_true")
    ingest_file.set_defaults(handler=_handle_ingest_file)

    record = subparsers.add_parser("record", help="Inspect or update canonical records")
    record.set_defaults(handler=_handle_missing_operation)
    record_subparsers = record.add_subparsers(dest="record_command")
    record_show = record_subparsers.add_parser("show", help="Show record metadata")
    record_show.add_argument("job_key")
    record_show.add_argument("--json", action="store_true")
    record_show.set_defaults(handler=_handle_record_show)
    record_status = record_subparsers.add_parser("set-status", help="Update record status axes")
    record_status.add_argument("job_key")
    record_status.add_argument("--application-status", choices=[item.value for item in ApplicationStatus])
    record_status.add_argument("--posting-status", choices=[item.value for item in PostingStatus])
    record_status.add_argument("--application-status-updated-at")
    record_status.add_argument("--application-note")
    record_status.add_argument("--json", action="store_true")
    record_status.set_defaults(handler=_handle_record_set_status)
    record_verdict = record_subparsers.add_parser("set-verdict", help="Update screening verdict metadata")
    record_verdict.add_argument("job_key")
    record_verdict.add_argument("verdict", choices=[item.value for item in ScreeningVerdict])
    record_verdict.add_argument("--json", action="store_true")
    record_verdict.set_defaults(handler=_handle_record_set_verdict)
    record_check_closed = record_subparsers.add_parser(
        "check-closed", help="Probe live posting status and close stale records"
    )
    record_check_closed.add_argument(
        "job_key", nargs="*", default=[],
        help="Optional platform:job_id keys to check individually",
    )
    record_check_closed.add_argument("--apply", action="store_true")
    record_check_closed.add_argument("--delay", type=_non_negative_float, default=1.0)
    record_check_closed.add_argument("--platform", action="append")
    record_check_closed.add_argument("--json", action="store_true")
    record_check_closed.add_argument(
        "--recheck-closed", action="store_true", dest="recheck_closed"
    )
    record_check_closed.set_defaults(handler=_handle_record_check_closed)

    queue = subparsers.add_parser("queue", help="Inspect queue and classification flows")
    queue.set_defaults(handler=_handle_missing_operation)
    queue_subparsers = queue.add_subparsers(dest="queue_command")
    queue_status = queue_subparsers.add_parser("status", help="Show runtime queue status")
    queue_status.add_argument("--json", action="store_true")
    queue_status.set_defaults(handler=_handle_queue_status)
    queue_migrate = queue_subparsers.add_parser("migrate-status", help="Summarize canonical status migration")
    queue_migrate.add_argument("--json", action="store_true")
    queue_migrate.set_defaults(handler=_handle_queue_migrate_status)
    queue_classify = queue_subparsers.add_parser("classify", help="Classify one canonical record")
    queue_classify.add_argument("job_key")
    queue_classify.add_argument("--dry-run", action="store_true")
    queue_classify.add_argument("--json", action="store_true")
    queue_classify.set_defaults(handler=_handle_queue_classify)
    queue_rescreen = queue_subparsers.add_parser("rescreen", help="Rescreen one canonical record")
    queue_rescreen.add_argument("job_key")
    queue_rescreen.add_argument("--dry-run", action="store_true")
    queue_rescreen.add_argument("--json", action="store_true")
    queue_rescreen.set_defaults(handler=_handle_queue_rescreen)
    queue_capped = queue_subparsers.add_parser(
        "capped", help="List or rescreen records capped by a local provider"
    )
    capped_mode = queue_capped.add_mutually_exclusive_group()
    capped_mode.add_argument("--list", action="store_true", dest="list_only")
    capped_mode.add_argument("--rescreen", action="store_true")
    queue_capped.add_argument("--limit", type=_positive_int)
    queue_capped.add_argument("--json", action="store_true")
    queue_capped.set_defaults(handler=_handle_queue_capped)
    queue_fallback = queue_subparsers.add_parser(
        "fallback", help="List or rescreen fallback screening documents (LLM failure → auto-hold)"
    )
    fallback_mode = queue_fallback.add_mutually_exclusive_group()
    fallback_mode.add_argument("--list", action="store_true", dest="list_only")
    fallback_mode.add_argument("--rescreen", action="store_true")
    queue_fallback.add_argument("--limit", type=_positive_int)
    queue_fallback.add_argument("--include-closed", action="store_true")
    queue_fallback.add_argument("--json", action="store_true")
    queue_fallback.set_defaults(handler=_handle_queue_fallback)
    queue_prescreened = queue_subparsers.add_parser(
        "prescreened", help="List or screen records awaiting screening (set aside by pre-screen)"
    )
    prescreened_mode = queue_prescreened.add_mutually_exclusive_group()
    prescreened_mode.add_argument("--list", action="store_true", dest="list_only")
    prescreened_mode.add_argument("--screen", action="store_true")
    queue_prescreened.add_argument("--limit", type=_positive_int)
    queue_prescreened.add_argument("--reason")
    queue_prescreened.add_argument(
        "--include-legacy",
        action="store_true",
        help=(
            "Also screen legacy verdict-bearing records. Set-aside records are screened "
            "first and fill --limit before any legacy record is reached."
        ),
    )
    queue_prescreened.add_argument("--json", action="store_true")
    queue_prescreened.set_defaults(handler=_handle_queue_prescreened)

    storage = subparsers.add_parser("storage", help="Read-only storage checks and status")
    storage.set_defaults(handler=_handle_missing_operation)
    storage_subparsers = storage.add_subparsers(dest="storage_command")
    storage_preflight = storage_subparsers.add_parser("preflight", help="Read-only storage preflight")
    storage_preflight.add_argument("--json", action="store_true")
    storage_preflight.set_defaults(handler=_handle_storage_preflight)
    storage_status = storage_subparsers.add_parser("status", help="Status counts from canonical records")
    storage_status.add_argument("--json", action="store_true")
    storage_status.set_defaults(handler=_handle_storage_status)
    storage_closed = storage_subparsers.add_parser(
        "backfill-closed",
        help="Preview or apply posting closure markers",
    )
    storage_closed.add_argument("--apply", action="store_true")
    storage_closed.add_argument("--json", action="store_true")
    storage_closed.set_defaults(handler=_handle_storage_backfill_closed)

    index = subparsers.add_parser("index", help="Manage derived search index")
    index.set_defaults(handler=_handle_missing_operation)
    index_subparsers = index.add_subparsers(dest="index_command")
    index_rebuild = index_subparsers.add_parser("rebuild", help="Rebuild derived sqlite index")
    index_rebuild.add_argument("--json", action="store_true")
    index_rebuild.add_argument("--database-path", type=Path)
    index_rebuild.set_defaults(handler=_handle_index_rebuild)

    summary = subparsers.add_parser("summary", help="Rebuild derived screening summary")
    summary.set_defaults(handler=_handle_missing_operation)
    summary_subparsers = summary.add_subparsers(dest="summary_command")
    summary_rebuild = summary_subparsers.add_parser("rebuild", help="Regenerate summary markdown")
    summary_rebuild.add_argument("--json", action="store_true")
    summary_rebuild.add_argument("--output", type=Path)
    summary_rebuild.set_defaults(handler=_handle_summary_rebuild)
    summary_stale = summary_subparsers.add_parser(
        "stale-screenings",
        help="Write a stale screening CSV report",
    )
    summary_stale.add_argument("--days", type=int, default=30)
    summary_stale.add_argument("--output", type=Path)
    summary_stale.add_argument("--json", action="store_true")
    summary_stale.set_defaults(handler=_handle_stale_screenings)

    screening = subparsers.add_parser("screening", help="Validate or run screening explicitly")
    screening.set_defaults(handler=_handle_missing_operation)
    screening_subparsers = screening.add_subparsers(dest="screening_command")
    screening_lint_parser = screening_subparsers.add_parser("lint", help="Lint canonical screening content")
    screening_lint_parser.add_argument("--hook", action="store_true")
    screening_lint_parser.add_argument("--file", action="append", default=[])
    screening_lint_parser.add_argument("--all", action="store_true")
    screening_lint_parser.add_argument("--json", action="store_true")
    screening_lint_parser.set_defaults(handler=_handle_screening_lint)
    screening_validate = screening_subparsers.add_parser("validate", help="Validate one screening markdown file")
    screening_validate.add_argument("path", type=Path)
    screening_validate.add_argument("--json", action="store_true")
    screening_validate.set_defaults(handler=_handle_screening_validate)
    screening_run = screening_subparsers.add_parser("run", help="Run screening for one canonical record")
    screening_run.add_argument("job_key")
    screening_run.add_argument("--company-file", type=Path)
    screening_run.add_argument("--candidate-context-file", type=Path)
    screening_run.add_argument("--dry-run", action="store_true")
    screening_run.add_argument("--json", action="store_true")
    screening_run.set_defaults(handler=_handle_screening_run)

    semantic_eval = subparsers.add_parser('semantic-eval', help='Capture, run, or compare semantic eval artifacts')
    semantic_eval.set_defaults(handler=_handle_missing_operation)
    semantic_eval_subparsers = semantic_eval.add_subparsers(dest='semantic_eval_command')
    semantic_eval_capture = semantic_eval_subparsers.add_parser('capture', help='Capture an unlabeled semantic eval queue')
    semantic_eval_capture.add_argument('--output', type=Path, required=True)
    semantic_eval_capture.add_argument('--seed', type=int)
    semantic_eval_capture.add_argument('--json', action='store_true')
    semantic_eval_capture.set_defaults(handler=_handle_semantic_eval_capture)
    semantic_eval_run = semantic_eval_subparsers.add_parser('run', help='Run semantic eval on a labeled dataset')
    semantic_eval_run.add_argument('--dataset', type=Path, required=True)
    semantic_eval_run.add_argument('--output', type=Path, required=True)
    semantic_eval_run.add_argument('--json', action='store_true')
    semantic_eval_run.set_defaults(handler=_handle_semantic_eval_run)
    semantic_eval_compare = semantic_eval_subparsers.add_parser('compare', help='Compare incumbent and candidate semantic eval reports')
    semantic_eval_compare.add_argument('--dataset', type=Path, required=True)
    semantic_eval_compare.add_argument('--incumbent', type=Path, required=True)
    semantic_eval_compare.add_argument('--candidate', type=Path, required=True)
    semantic_eval_compare.add_argument('--output', type=Path)
    semantic_eval_compare.add_argument('--json', action='store_true')
    semantic_eval_compare.set_defaults(handler=_handle_semantic_eval_compare)

    link = subparsers.add_parser("link", help="Cross-platform JD link groups")
    link.set_defaults(handler=_handle_missing_operation)
    link_subparsers = link.add_subparsers(dest="link_command")
    link_add = link_subparsers.add_parser("add", help="Link records into a group")
    link_add.add_argument("keys", nargs="+")
    link_add.add_argument("--note")
    link_add.add_argument("--json", action="store_true")
    link_add.set_defaults(handler=_handle_link_add)
    link_remove = link_subparsers.add_parser("remove", help="Remove a record from its link group")
    link_remove.add_argument("key")
    link_remove.add_argument("--json", action="store_true")
    link_remove.set_defaults(handler=_handle_link_remove)
    link_show = link_subparsers.add_parser("show", help="Show link group for a record")
    link_show.add_argument("key")
    link_show.add_argument("--json", action="store_true")
    link_show.set_defaults(handler=_handle_link_show)
    link_list = link_subparsers.add_parser("list", help="List all link groups")
    link_list.add_argument("--inconsistent", action="store_true")
    link_list.add_argument("--json", action="store_true")
    link_list.set_defaults(handler=_handle_link_list)
    link_sync = link_subparsers.add_parser("sync", help="Sync status across a link group")
    link_sync.add_argument("key")
    link_sync.add_argument("--dry-run", action="store_true")
    link_sync.add_argument("--json", action="store_true")
    link_sync.set_defaults(handler=_handle_link_sync)

    console = subparsers.add_parser("console", help="Serve the local JD review console")
    console.set_defaults(handler=_handle_missing_operation)
    console_subparsers = console.add_subparsers(dest="console_command")
    console_serve = console_subparsers.add_parser("serve", help="Start the loopback-only JD console")
    console_serve.add_argument("--host", default="127.0.0.1")
    console_serve.add_argument("--port", type=int, default=8765)
    console_serve.set_defaults(handler=_handle_console_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_cli_logging(verbose=args.verbose, stream=sys.stderr)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help(sys.stdout)
        return 0
    try:
        workspace = resolve_workspace(explicit=args.workspace)
        services = _build_services(workspace)
        return handler(args, workspace, services)
    except (OSError, RuntimeError, ValueError, WorkspaceResolutionError) as exc:
        print(f"career-jobs: {exc}", file=sys.stderr)
        return 2


def _build_services(workspace: WorkspacePaths, *, http: HttpClient | None = None) -> ServiceBundle:
    maintenance = JobsMaintenanceService(workspace=workspace, http=http)
    pipeline = JobsPipelineService(
        workspace_root=workspace.root,
        repository=maintenance.repository,
        runtime_dir=maintenance.runtime_dir,
    )
    automation = AutomationService(
        search_port=maintenance,
        extraction_stage=JobsExtractionStage(repository=maintenance.repository),
        screening_stage=JobsScreeningStage(
            workspace=workspace,
            repository=maintenance.repository,
            candidate_context=load_candidate_context(workspace),
        ),
        completion_stage=JobsCompletionStage(
            pipeline=pipeline,
            maintenance=maintenance,
        ),
        resume_state=JobsResumeStateService(workspace=workspace),
        result_store=JobsAutoResultService(workspace=workspace),
    )
    links_dir = workspace.jobs_dir / "links"
    link_store = LinkStore(links_dir)
    link_service = LinkService(link_store=link_store, record_repo=maintenance.repository)
    return ServiceBundle(
        maintenance=maintenance,
        pipeline=pipeline,
        automation=automation,
        link_service=link_service,
    )


def _parse_job_key(raw: str) -> JobKey:
    if ":" not in raw:
        raise ValueError("job_key must be platform:job_id")
    platform, job_id = raw.split(":", 1)
    return JobKey(platform, job_id)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def _non_negative_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError("expected a finite non-negative number")
    return value


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _relative_workspace_root(workspace: WorkspacePaths) -> str:
    return "."


def _base_payload(command: str, workspace: WorkspacePaths) -> dict[str, Any]:
    return {
        "command": command,
        "workspace_root": _relative_workspace_root(workspace),
        "workspace_source": workspace.source,
    }


def _check_closed_payload(
    *,
    workspace: WorkspacePaths,
    result: CheckClosedResult,
    apply: bool,
    recheck: bool,
) -> dict[str, Any]:
    payload = _base_payload("record check-closed", workspace)
    payload.update(
        {
            "mode": "reopen" if recheck else "close",
            "apply": apply,
            "dry_run": not apply,
            "changed": result.changed,
            "closed_keys": list(result.closed_keys),
            "reopened_keys": list(result.reopened_keys),
            "unknown_keys": list(result.unknown_keys),
            "skipped_platform_counts": dict(result.skipped_platform_counts),
            "tripped_platforms": list(result.tripped_platforms),
        }
    )
    return payload


def _print_check_closed_human(payload: dict[str, Any]) -> None:
    def _boolean(name: str) -> str:
        return str(payload[name]).lower()

    print(
        f"mode={payload['mode']} apply={_boolean('apply')} "
        f"dry_run={_boolean('dry_run')} changed={_boolean('changed')}"
    )
    sections = (
        ("closed", payload["closed_keys"]),
        ("reopened", payload["reopened_keys"]),
        ("unknown", payload["unknown_keys"]),
    )
    for label, entries in sections:
        print(f"{label} ({len(entries)}):")
        if entries:
            for entry in entries:
                print(f"- {entry}")
        else:
            print("- none")

    skipped = payload["skipped_platform_counts"]
    print(f"skipped ({len(skipped)}):")
    if skipped:
        for platform in sorted(skipped):
            print(f"- {platform}: {skipped[platform]}")
    else:
        print("- none")

    tripped = payload["tripped_platforms"]
    print(f"tripped ({len(tripped)}):")
    if tripped:
        for platform in tripped:
            print(f"- {platform}")
    else:
        print("- none")


def _payload_from_ingest(command: str, workspace: WorkspacePaths, result: IngestResult) -> dict[str, Any]:
    platform = get_platform_from_url(result.source)
    if platform is None:
        platform = result.source.replace(":", "/").split("/", 1)[0]
        if platform not in {"wanted", "remember", "groupby", "saramin", "thevc"}:
            platform = None
    payload = _base_payload(command, workspace)
    payload.update(
        {
            "job_key": (
                f"{platform}:{result.job_id}"
                if platform is not None and result.job_id is not None
                else result.job_id
            ),
            "outcome": result.outcome,
            "message": result.message,
            "target": result.target,
            "current": result.current,
            "verdict": result.verdict,
        }
    )
    return payload


def _print_automation_result(result: AutomationRunResult) -> int:
    if result.stdout:
        print(result.stdout, end="", file=sys.stdout)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _handle_missing_operation(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    del workspace, services
    print(f"career-jobs {args.command}: operation required", file=sys.stderr)
    return 2


def _handle_run_auto(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    automation_args: list[str] = []
    if args.from_urls is not None:
        source = (
            args.from_urls
            if args.from_urls.is_absolute()
            else workspace.root / args.from_urls
        )
        automation_args.extend(["--from-urls", str(source)])
    if args.search_only:
        automation_args.append("--search-only")
    if args.screening_only:
        automation_args.append("--screening-only")
    if args.dry_run:
        automation_args.append("--dry-run")
    if args.resume:
        automation_args.append("--resume")
    if args.max_urls is not None:
        automation_args.extend(["--max-urls", str(args.max_urls)])
    if args.llm_timeout != 120:
        automation_args.extend(["--llm-timeout", str(args.llm_timeout)])
    if args.local_llm_timeout is not None:
        automation_args.extend(
            ["--local-llm-timeout", str(args.local_llm_timeout)]
        )
    if args.no_classify:
        automation_args.append("--no-classify")
    if args.json:
        automation_args.append("--json")
    return _print_automation_result(services.automation.run("auto", automation_args))

def _handle_config_check(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.config_check()
    if args.json:
        payload = _base_payload("config check", workspace)
        payload.update(
            {
                "status": "ok" if result.ready else "needs_action",
                "ready": result.ready,
                "action": result.action,
                "normalized_role": result.normalized_role,
                "finding_codes": [item.code for item in result.findings],
            }
        )
        _print_json(payload)
    else:
        print(f"ready={result.ready} action={result.action} normalized_role={result.normalized_role}")
        for finding in result.findings:
            print(f"- {finding.code}: {finding.message}")
    return 0 if result.ready else 2


def _handle_config_preview(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.config_preview()
    if args.json:
        payload = _base_payload("config preview", workspace)
        payload.update(
            {
                "status": "ok" if result.action != "reject" else "rejected",
                "ready": result.ready,
                "action": result.action,
                "normalized_role": result.normalized_role,
                "finding_codes": [item.code for item in result.diagnostics],
                "would_write": result.action == "apply",
            }
        )
        _print_json(payload)
    else:
        print(f"ready={result.ready} action={result.action} normalized_role={result.normalized_role}")
        for finding in result.diagnostics:
            print(f"- {finding.code}: {finding.message}")
    return 0 if result.action != "reject" else 2


def _handle_config_apply(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.config_apply()
    if args.json:
        payload = _base_payload("config apply", workspace)
        payload.update(
            {
                "status": "ok" if result.action != "reject" else "rejected",
                "action": result.action,
                "changed": result.changed,
                "backup_created": result.backup_path is not None,
                "finding_codes": [item.code for item in result.diagnostics],
            }
        )
        _print_json(payload)
    else:
        print(f"action={result.action} changed={result.changed} backup_created={result.backup_path is not None}")
        for finding in result.diagnostics:
            print(f"- {finding.code}: {finding.message}")
    return 0 if result.action != "reject" else 2


def _handle_search_run(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.search(queries=args.query, max_urls=args.max_urls)
    if not args.dry_run:
        services.maintenance.persist_seen_job_keys(
            result.updated_seen_job_keys,
            new_count=len(result.postings),
        )
    if args.json:
        payload = _base_payload("search run", workspace)
        payload.update(
            {
                "status": "ok",
                "postings": [candidate.seen_key for candidate in result.postings],
                "counts": {
                    "total_found": result.total_found,
                    "returned": len(result.postings),
                    "filtered_out": result.filtered_out,
                    "duplicates": result.duplicates,
                },
                "capabilities": result.capabilities,
                "diagnostics": list(result.diagnostics),
            }
        )
        _print_json(payload)
    else:
        print(
            f"returned={len(result.postings)} total_found={result.total_found} filtered_out={result.filtered_out} duplicates={result.duplicates}"
        )
        for candidate in result.postings:
            print(f"- {candidate.seen_key} {candidate.company} | {candidate.title} | {candidate.url}")
        for diagnostic in result.diagnostics:
            print(f"! {diagnostic}", file=sys.stderr)
    return 0


def _handle_search_status(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.search_status()
    if args.json:
        payload = _base_payload("search status", workspace)
        payload.update(
            {
                "status": "ok",
                "last_run": result.last_run,
                "counts": {
                    "total_searches": result.total_searches,
                    "total_new_found": result.total_new_found,
                    "tracked_job_keys": result.tracked_job_keys,
                },
            }
        )
        _print_json(payload)
    else:
        print(f"last_run={result.last_run or 'none'}")
        print(f"total_searches={result.total_searches}")
        print(f"total_new_found={result.total_new_found}")
        print(f"tracked_job_keys={result.tracked_job_keys}")
    return 0


def _handle_search_reset(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    changed = services.maintenance.reset_search_state()
    if args.json:
        payload = _base_payload("search reset-state", workspace)
        payload.update({"status": "ok", "changed": changed})
        _print_json(payload)
    else:
        print(f"changed={changed}")
    return 0


def _handle_company_validate(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    summary = services.maintenance.company_validate(file_name=args.file, fix=args.fix)
    print("기업정보 검증 완료")
    print(
        "processed="
        f"{summary.processed_files} errors={summary.error_files} "
        f"critical={summary.critical_risk_companies} high={summary.high_risk_companies} "
        f"incomplete={summary.incomplete_companies}"
    )
    for result in summary.results:
        print(
            f"- {result.file_path.name}: completeness={result.completeness_score:.0f}% "
            f"startup={'yes' if result.data.is_startup else 'no'} "
            f"risks={len(result.risk_flags)} warnings={len(result.issues)}"
        )
    for fixed in summary.fixed_files:
        print(f"fixed: {fixed}")
    for error in summary.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0 if summary.error_files == 0 else 2


def _handle_company_apply(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    del services
    try:
        markdown = args.input.read_text(encoding="utf-8")
        result = CompanyInfoService(workspace=workspace).apply_candidate(
            company_name=args.company_name,
            markdown=markdown,
            expected_digest=args.expected_digest,
            timeout=args.timeout,
        )
    except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        payload = _base_payload("company apply", workspace)
        payload.update(
            {
                "status": result.status,
                "persisted": True,
                "completeness": (
                    None if result.validation is None else round(result.validation.completeness_score, 1)
                ),
                "file_path": (
                    None
                    if result.file_path is None
                    else str(result.file_path.relative_to(workspace.root))
                ),
            }
        )
        _print_json(payload)
    else:
        completeness = None if result.validation is None else f"{result.validation.completeness_score:.0f}%"
        print(f"company apply complete: status={result.status} completeness={completeness}")
    return 0


def _handle_company_fetch(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    del workspace, services
    from dataclasses import asdict

    if args.platform == "remember":
        from careerkit.jobs.adapters.platforms.remember import format_company_markdown as remember_markdown
        from careerkit.jobs.adapters.platforms.remember import remember_company_http

        try:
            info = remember_company_http(args.company_id)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            result = asdict(info)
            result["employee_stats"] = list(result["employee_stats"])
            result["tags"] = list(result["tags"])
            _print_json(result)
        else:
            print(remember_markdown(info))
    elif args.platform == "saramin":
        from careerkit.jobs.adapters.platforms.saramin import format_company_markdown, saramin_company_http

        try:
            info = saramin_company_http(args.company_id)
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(asdict(info))
        else:
            print(format_company_markdown(info))
    elif args.platform == "wanted":
        from careerkit.jobs.adapters.platforms.wanted import format_wanted_company_markdown, wanted_company_http

        try:
            company_id = _positive_int(args.company_id)
        except (TypeError, ValueError, argparse.ArgumentTypeError):
            print("error: Wanted company id must be a positive integer", file=sys.stderr)
            return 1

        try:
            info = wanted_company_http(company_id)
        except (ValueError, OSError, RuntimeError):
            print("error: failed to fetch Wanted company info", file=sys.stderr)
            return 1
        if args.json:
            result = asdict(info)
            result["tags"] = list(result["tags"])
            _print_json(result)
        else:
            print(format_wanted_company_markdown(info))
    else:
        print(f"error: unsupported platform: {args.platform}", file=sys.stderr)
        return 1
    return 0


def _handle_ingest_url(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.pipeline.ingest_url(args.url)
    if args.json:
        _print_json(_payload_from_ingest("ingest url", workspace, result))
    else:
        print(f"{result.outcome}: {result.message}")
    return 0 if result.outcome != "error" else 2


def _handle_ingest_file(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    results = services.pipeline.ingest_file(args.path)
    if args.json:
        payload = _base_payload("ingest file", workspace)
        payload.update(
            {
                "status": "ok",
                "count": len(results),
                "items": [
                    {
                        "job_key": item.job_id,
                        "outcome": item.outcome,
                        "message": item.message,
                    }
                    for item in results
                ],
            }
        )
        _print_json(payload)
    else:
        for item in results:
            print(f"{item.outcome}: {item.message}")
    return 0


def _handle_record_show(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    key = _parse_job_key(args.job_key)
    stored = services.pipeline.show_record(key)
    payload = {
        "job_key": f"{stored.record.platform}:{stored.record.job_id}",
        "has_screening": stored.has_screening,
        "screening_verdict": stored.record.screening_verdict.value if stored.record.screening_verdict else None,
        "prescreen_reason": stored.record.prescreen_reason,
        "application_status": stored.record.application_status.value,
        "posting_status": stored.record.posting_status.value,
        "application_status_updated_at": stored.record.application_status_updated_at,
        "application_history": [
            {
                "status": event.status.value,
                "occurred_at": event.occurred_at,
                "note": event.note,
            }
            for event in stored.record.application_history
        ],
        "schema_version": stored.record.schema_version,
    }
    if args.json:
        base = _base_payload("record show", workspace)
        base.update(payload)
        _print_json(base)
    else:
        history = payload.pop("application_history")
        for key_name, value in payload.items():
            print(f"{key_name}={value}")
        for index, event in enumerate(history, start=1):
            line = (
                f"application_history[{index}]="
                f"{event['occurred_at']} {event['status']}"
            )
            if event["note"] is not None:
                line = f"{line} note={event['note']}"
            print(line)
    return 0


def _handle_record_set_status(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    key = _parse_job_key(args.job_key)
    if args.application_status is None and args.application_status_updated_at is not None:
        raise ValueError("application status is required when application_status_updated_at is set")
    if args.application_status is None and args.application_note is not None:
        raise ValueError("application status is required when application_note is set")
    if args.application_status is None and args.posting_status is None:
        raise ValueError("set-status requires --application-status and/or --posting-status")
    updated = services.pipeline.set_record_status(
        key,
        application_status=ApplicationStatus(args.application_status) if args.application_status else None,
        posting_status=PostingStatus(args.posting_status) if args.posting_status else None,
        application_status_updated_at=args.application_status_updated_at,
        application_note=args.application_note,
    )
    if args.json:
        payload = _base_payload("record set-status", workspace)
        payload.update(
            {
                "job_key": f"{updated.record.platform}:{updated.record.job_id}",
                "application_status": updated.record.application_status.value,
                "posting_status": updated.record.posting_status.value,
                "application_status_updated_at": updated.record.application_status_updated_at,
                "application_history": [
                    {
                        "status": event.status.value,
                        "occurred_at": event.occurred_at,
                        "note": event.note,
                    }
                    for event in updated.record.application_history
                ],
            }
        )
        _print_json(payload)
    else:
        print(
            f"updated {updated.record.platform}:{updated.record.job_id} application={updated.record.application_status.value} posting={updated.record.posting_status.value}"
        )
    if services.link_service is not None:
        group = services.link_service.check_membership(key)
        if group is not None:
            print(
                f"ℹ 이 레코드는 링크 그룹 {group.group_id}에 속해 있습니다. "
                f"career-jobs link sync {key.platform}:{key.job_id} 로 동기화하세요.",
                file=sys.stderr,
            )
    return 0


def _handle_record_set_verdict(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    key = _parse_job_key(args.job_key)
    updated = services.pipeline.set_record_verdict(key, ScreeningVerdict(args.verdict))
    if args.json:
        payload = _base_payload("record set-verdict", workspace)
        payload.update(
            {
                "job_key": f"{updated.record.platform}:{updated.record.job_id}",
                "screening_verdict": updated.record.screening_verdict.value if updated.record.screening_verdict else None,
            }
        )
        _print_json(payload)
    else:
        print(f"updated {updated.record.platform}:{updated.record.job_id} verdict={updated.record.screening_verdict.value if updated.record.screening_verdict else None}")
    return 0


def _parse_job_key(raw: str) -> JobKey:
    parts = raw.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"invalid job key {raw!r}; expected platform:job_id")
    return JobKey(platform=parts[0], job_id=parts[1])


def _handle_record_check_closed(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    platforms = tuple(args.platform) if args.platform else None
    if platforms:
        unsupported = sorted(set(platforms) - SUPPORTED_PLATFORMS)
        if unsupported:
            raise ValueError(
                f"unsupported platform(s): {', '.join(unsupported)}; "
                f"supported: {', '.join(sorted(SUPPORTED_PLATFORMS))}"
            )
    keys: tuple[JobKey, ...] | None = None
    if args.job_key:
        keys = tuple(_parse_job_key(k) for k in args.job_key)
        if platforms is not None:
            raise ValueError("--platform and positional job keys are mutually exclusive")
        unsupported_key_platforms = sorted({k.platform for k in keys} - SUPPORTED_PLATFORMS)
        if unsupported_key_platforms:
            raise ValueError(
                f"unsupported platform(s) in job keys: {', '.join(unsupported_key_platforms)}; "
                f"supported: {', '.join(sorted(SUPPORTED_PLATFORMS))}"
            )
    result = services.maintenance.check_closed(
        dry_run=not args.apply, delay=args.delay, platforms=platforms,
        recheck=args.recheck_closed, keys=keys,
    )
    payload = _check_closed_payload(
        workspace=workspace,
        result=result,
        apply=args.apply,
        recheck=args.recheck_closed,
    )
    if args.json:
        _print_json(payload)
        return 0
    _print_check_closed_human(payload)
    return 0


def _handle_queue_status(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.pipeline.queue_status()
    if args.json:
        payload = _base_payload("queue status", workspace)
        payload.update({"total": result.total, "counts": result.counts})
        _print_json(payload)
    else:
        print(f"total={result.total}")
        for name, count in result.counts.items():
            print(f"- {name}: {count}")
    return 0


def _handle_queue_migrate_status(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.pipeline.migrate_queue_status()
    if args.json:
        payload = _base_payload("queue migrate-status", workspace)
        payload.update({"count": len(result), "items": result})
        _print_json(payload)
    else:
        for item in result:
            print(f"{item['job_key']} {item['status']} {item['result']}")
    return 0


def _handle_queue_classify(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.pipeline.classify_record(_parse_job_key(args.job_key), dry_run=args.dry_run)
    if args.json:
        _print_json(_payload_from_ingest("queue classify", workspace, result))
    else:
        print(f"{result.outcome}: {result.message}")
    return 0 if result.outcome != "error" else 2


def _rescreen_one(
    key: JobKey,
    workspace: WorkspacePaths,
    services: ServiceBundle,
    *,
    dry_run: bool,
    require_strong_provider: bool = False,
) -> IngestResult:
    """Rescreen a single record through the normal screening path."""
    repository = JDRecordRepository(workspace.jobs_records_dir)
    stored = repository.get(key)
    company_info = CompanyInfoService(workspace=workspace)
    company_file = company_info.find_matching_file(stored.record.company)
    if company_file is not None:
        validation = company_info.validate(file_name=str(company_file))
        if validation.errors or validation.incomplete_companies:
            raise ValueError("company info is invalid or incomplete")
    screening = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=company_file,
        llm_timeout=120,
        dry_run=dry_run,
        repository=None if dry_run else repository,
        candidate_context=load_candidate_context(workspace),
        require_strong_provider=require_strong_provider,
    )
    if not dry_run and not screening.published:
        if screening.used_fallback:
            reason = "no provider answered"
        else:
            reason = f"still capped by {screening.provider}"
        return IngestResult(
            source=f"{key.platform}:{key.job_id}",
            job_id=key.job_id,
            outcome="skipped",
            message=f"{reason} — stored screening left untouched",
            verdict=screening.verdict,
        )
    if dry_run:
        return IngestResult(
            source=f"{key.platform}:{key.job_id}",
            job_id=key.job_id,
            outcome="success",
            message=f"[DRY-RUN] rescreen → {screening.verdict}",
            verdict=screening.verdict,
        )
    return services.pipeline.classify_record(key, dry_run=False)


def _handle_queue_rescreen(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    key = _parse_job_key(args.job_key)
    results = [_rescreen_one(key, workspace, services, dry_run=args.dry_run)]
    if args.json:
        payload = _base_payload("queue rescreen", workspace)
        payload.update({"count": len(results), "items": [_payload_from_ingest("queue rescreen", workspace, item) for item in results]})
        _print_json(payload)
    else:
        for item in results:
            print(f"{item.outcome}: {item.message}")
    return 0


def _run_batch_rescreen(
    records: list[Any],
    workspace: WorkspacePaths,
    services: ServiceBundle,
    repository: JDRecordRepository,
    recovered_predicate: Any,
    *,
    unrecovered_label: str,
    require_strong_provider: bool = True,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    # require_strong_provider keeps a local answer from replacing the stored
    # screening: the revision store is latest-only, so republishing an equally
    # weak verdict would destroy the existing document for nothing.
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for record in records:
        key = JobKey(record.platform, record.job_id)
        name = f"{key.platform}:{key.job_id}"
        # One bad record must not abort the batch.
        try:
            result = _rescreen_one(
                key, workspace, services, dry_run=False, require_strong_provider=require_strong_provider
            )
        except Exception as exc:
            # The classification outcome (from classify_record) answers
            # "was it reclassified", not "was the predicate satisfied".
            # A strong provider may have already published a new screening
            # before classify_record threw; check the predicate to distinguish
            # a true failure from a classify-only failure.
            try:
                published = recovered_predicate(key, repository)
            except Exception:
                published = False
            label = "failed_after_publish" if published else "failed"
            counts[label] += 1
            items.append({"job_key": name, "outcome": label, "message": f"{name}: {exc}"})
            continue
        recovered = recovered_predicate(key, repository)
        outcome = "rescreened" if recovered else unrecovered_label
        counts[outcome] += 1
        items.append(
            {
                "job_key": name,
                "outcome": outcome,
                "classification": result.outcome,
                "verdict": result.verdict,
                "message": f"{name}: {result.message}",
            }
        )
    return items, counts


def _handle_queue_capped(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    repository = JDRecordRepository(workspace.jobs_records_dir)
    capped = [item.record for item in repository.list_metadata() if item.record.verdict_capped]
    if args.limit is not None:
        capped = capped[: args.limit]

    if not args.rescreen:
        if args.json:
            payload = _base_payload("queue capped", workspace)
            payload.update(
                {
                    "count": len(capped),
                    "items": [
                        {
                            "job_key": f"{record.platform}:{record.job_id}",
                            "company": record.company,
                            "screening_provider": record.screening_provider,
                        }
                        for record in capped
                    ],
                }
            )
            _print_json(payload)
        else:
            print(f"{len(capped)} records capped by local provider:")
            for record in capped:
                print(f"  {record.platform}:{record.job_id}  ({record.screening_provider})")
        return 0

    def _capped_recovered(key: JobKey, repo: JDRecordRepository) -> bool:
        return not repo.get_metadata(key).record.verdict_capped

    items, counts = _run_batch_rescreen(
        capped, workspace, services, repository,
        _capped_recovered,
        unrecovered_label="still_capped",
    )

    if args.json:
        payload = _base_payload("queue capped", workspace)
        payload.update(
            {
                "count": len(items),
                "rescreened": counts["rescreened"],
                "still_capped": counts["still_capped"],
                "failed": counts["failed"],
                "failed_after_publish": counts["failed_after_publish"],
                "items": items,
            }
        )
        _print_json(payload)
    else:
        for item in items:
            print(item["message"])
    return 2 if counts["failed"] else 0


def _select_fallback_records(
    repository: JDRecordRepository,
    *,
    include_closed: bool = False,
) -> tuple[list[Any], int, int]:
    """Return (selected_records, skipped_closed_count, unreadable_count)."""
    skipped_closed = 0
    unreadable = 0
    selected = []
    for item in repository.list_metadata():
        if not item.has_screening:
            continue
        try:
            stored = repository.get(item.record.key)
        except Exception:
            unreadable += 1
            continue
        md = stored.screening_markdown
        if md is None or not is_fallback_document(md):
            continue
        if not include_closed and stored.record.posting_status != PostingStatus.ACTIVE:
            skipped_closed += 1
            continue
        selected.append(stored.record)
    return selected, skipped_closed, unreadable


def _handle_queue_fallback(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    repository = JDRecordRepository(workspace.jobs_records_dir)
    selected, skipped_closed, unreadable = _select_fallback_records(
        repository, include_closed=args.include_closed,
    )
    if args.limit is not None:
        selected = selected[: args.limit]

    if not args.rescreen:
        if args.json:
            payload = _base_payload("queue fallback", workspace)
            payload.update(
                {
                    "count": len(selected),
                    "skipped_closed": skipped_closed,
                    "unreadable": unreadable,
                    "items": [
                        {
                            "job_key": f"{record.platform}:{record.job_id}",
                            "company": record.company,
                            "posting_status": record.posting_status.value,
                        }
                        for record in selected
                    ],
                }
            )
            _print_json(payload)
        else:
            print(f"{len(selected)} fallback screening documents (skipped {skipped_closed} closed):")
            for record in selected:
                print(f"  {record.platform}:{record.job_id}  {record.company}")
            if unreadable:
                print(f"({unreadable} records unreadable)")
        return 0

    providers = resolve_commands()
    if not any(label in STRONG_PROVIDER_LABELS for label, _cmd in providers):
        msg = "No strong provider available (need claude or codex). Aborting."
        if args.json:
            payload = _base_payload("queue fallback", workspace)
            payload["error"] = msg
            _print_json(payload)
        else:
            print(msg, file=sys.stderr)
        return 1

    def _fallback_recovered(key: JobKey, repo: JDRecordRepository) -> bool:
        stored = repo.get(key)
        md = stored.screening_markdown
        return md is not None and not is_fallback_document(md)

    items, counts = _run_batch_rescreen(
        selected, workspace, services, repository,
        _fallback_recovered,
        unrecovered_label="still_fallback",
    )

    if args.json:
        payload = _base_payload("queue fallback", workspace)
        payload.update(
            {
                "count": len(items),
                "rescreened": counts["rescreened"],
                "still_fallback": counts["still_fallback"],
                "failed": counts["failed"],
                "failed_after_publish": counts["failed_after_publish"],
                "skipped_closed": skipped_closed,
                "unreadable": unreadable,
                "items": items,
            }
        )
        _print_json(payload)
    else:
        for item in items:
            print(item["message"])
        if skipped_closed:
            print(f"(skipped {skipped_closed} closed postings)")
    return 2 if counts["failed"] or counts["failed_after_publish"] else 0


def _handle_queue_prescreened(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    listing = services.pipeline.list_prescreened(reason=args.reason)
    set_aside = listing.set_aside
    legacy = listing.legacy
    # The counts report the whole backlog; --limit only bounds what is shown or
    # screened. A header reading the sliced length would hide the backlog this
    # command exists to surface.
    set_aside_total = len(set_aside)
    legacy_total = len(legacy)
    if args.limit is not None:
        set_aside = set_aside[: args.limit]
        legacy = legacy[: args.limit]

    if not args.screen:
        if args.json:
            payload = _base_payload("queue prescreened", workspace)
            payload.update(
                {
                    "set_aside_count": set_aside_total,
                    "legacy_count": legacy_total,
                    "set_aside_shown": len(set_aside),
                    "legacy_shown": len(legacy),
                    "set_aside": [
                        {
                            "job_key": f"{item.record.platform}:{item.record.job_id}",
                            "company": item.record.company,
                            "prescreen_reason": item.record.prescreen_reason,
                        }
                        for item in set_aside
                    ],
                    "legacy": [
                        {
                            "job_key": f"{item.record.platform}:{item.record.job_id}",
                            "company": item.record.company,
                            "screening_verdict": (
                                item.record.screening_verdict.value
                                if item.record.screening_verdict is not None
                                else None
                            ),
                        }
                        for item in legacy
                    ],
                }
            )
            _print_json(payload)
        else:
            set_aside_shown = f" (showing {len(set_aside)})" if len(set_aside) < set_aside_total else ""
            legacy_shown = f" (showing {len(legacy)})" if len(legacy) < legacy_total else ""
            print(f"{set_aside_total} records set aside by pre-screen{set_aside_shown}:")
            for item in set_aside:
                record = item.record
                print(f"  {record.platform}:{record.job_id}  {record.company}  ({record.prescreen_reason})")
            print(f"{legacy_total} legacy verdicts without a screening document{legacy_shown}:")
            for item in legacy:
                record = item.record
                verdict = record.screening_verdict.value if record.screening_verdict is not None else None
                print(f"  {record.platform}:{record.job_id}  {record.company}  ({verdict})")
        return 0

    if args.limit is None:
        backlog = set_aside_total + (legacy_total if args.include_legacy else 0)
        msg = f"--limit is required to screen ({backlog} records in the backlog). Aborting."
        if args.json:
            payload = _base_payload("queue prescreened", workspace)
            payload.update({"error": msg, "backlog": backlog})
            _print_json(payload)
        else:
            print(msg, file=sys.stderr)
        return 1

    targets = [item.record for item in set_aside]
    if args.include_legacy:
        targets.extend(item.record for item in legacy)
    targets = targets[: args.limit]
    # Set-aside records fill --limit first, so --include-legacy can reach nothing.
    # Say so: the user asked for legacy records and paid for a run without one.
    # Selected, not recovered: the notice is about --limit crowding legacy out,
    # not about a screening that failed. The JSON's legacy_screened counts documents.
    legacy_selected = max(0, len(targets) - len(set_aside))
    notice = None
    if args.include_legacy and legacy_total and not legacy_selected:
        notice = (
            f"--include-legacy screened no legacy record: {len(targets)} set-aside "
            f"records filled --limit {args.limit} ({legacy_total} legacy records waiting). "
            "Raise --limit above the set-aside backlog to reach them."
        )

    repository = JDRecordRepository(workspace.jobs_records_dir)

    def _prescreened_recovered(key: JobKey, repo: JDRecordRepository) -> bool:
        return repo.get_metadata(key).has_screening

    # A set-aside record holds no screening document, so the latest-only revision
    # store has nothing a weak answer could destroy; requiring a strong provider
    # would make every run a no-op whenever only a local one is reachable, and
    # `queue capped --rescreen` already recovers a locally capped verdict.
    items, counts = _run_batch_rescreen(
        targets, workspace, services, repository,
        _prescreened_recovered,
        unrecovered_label="still_unscreened",
        require_strong_provider=False,
    )

    # Count what actually got a document, not what was selected. Deriving these from
    # the target list makes them contradict `rescreened` / `failed` in the same
    # payload, and automation reading them cannot tell how much was recovered.
    set_aside_keys = {(item.record.platform, item.record.job_id) for item in set_aside}
    recovered_set_aside = 0
    recovered_legacy = 0
    for item in items:
        if item["outcome"] != "rescreened":
            continue
        platform, _, job_id = item["job_key"].partition(":")
        if (platform, job_id) in set_aside_keys:
            recovered_set_aside += 1
        else:
            recovered_legacy += 1

    if args.json:
        payload = _base_payload("queue prescreened", workspace)
        payload.update(
            {
                "count": len(items),
                "rescreened": counts["rescreened"],
                "still_unscreened": counts["still_unscreened"],
                "failed": counts["failed"],
                "failed_after_publish": counts["failed_after_publish"],
                "set_aside_screened": recovered_set_aside,
                "legacy_screened": recovered_legacy,
                "notice": notice,
                "items": items,
            }
        )
        _print_json(payload)
    else:
        for item in items:
            print(item["message"])
        if notice is not None:
            print(notice)
    # `failed_after_publish` is a failure the JSON already reports; exiting 0 on it
    # lets a script read a partially failed batch as clean. `queue fallback` counts
    # it, `queue capped` does not — this follows fallback.
    return 2 if counts["failed"] or counts["failed_after_publish"] else 0


def _handle_storage_preflight(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.storage_preflight()
    try:
        if args.json:
            payload = _base_payload("storage preflight", workspace)
            payload.update(
                {
                    "ready": result.ready,
                    "record_count": result.record_count,
                    "screening_count": result.screening_count,
                    "schema_version": result.schema_version,
                    "status_counts": result.status_counts,
                    "application_timestamp_categories": (
                        result.application_timestamp_categories
                    ),
                    "finding_codes": [item.code for item in result.findings],
                }
            )
            _print_json(payload)
        else:
            print(f"ready={result.ready} record_count={result.record_count} screening_count={result.screening_count}")
            for finding in result.findings:
                print(f"- {finding.code}")
        return 0 if result.ready else 2
    finally:
        services.maintenance.cleanup_preflight(result.isolated_output_root)


def _handle_storage_status(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.pipeline.storage_status()
    if args.json:
        payload = _base_payload("storage status", workspace)
        payload.update({"counts": result})
        _print_json(payload)
    else:
        for label, count in sorted(result.items()):
            print(f"{label}={count}")
    return 0


def _handle_storage_backfill_closed(
    args: argparse.Namespace,
    workspace: WorkspacePaths,
    services: ServiceBundle,
) -> int:
    result = services.maintenance.backfill_closed(dry_run=not args.apply)
    if args.json:
        payload = _base_payload("storage backfill-closed", workspace)
        payload.update(
            {
                "status": "ok",
                "apply": args.apply,
                "changed": result.changed,
                "job_keys": list(result.keys),
                "count": len(result.keys),
            }
        )
        _print_json(payload)
    else:
        print(f"apply={args.apply} changed={result.changed} count={len(result.keys)}")
        for key in result.keys:
            print(f"- {key}")
    return 0


def _handle_index_rebuild(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    report = services.maintenance.rebuild_index(database_path=args.database_path)
    target = args.database_path or (services.maintenance.derived_dir / "search.sqlite3")
    if args.json:
        payload = _base_payload("index rebuild", workspace)
        payload.update(
            {
                "success": report.success,
                "indexed_count": report.indexed_count,
                "database_path": services.maintenance.relative_path(target),
                "errors": [
                    {"job_key": f"{item.platform}:{item.job_id}", "message": item.message}
                    for item in report.errors
                ],
            }
        )
        _print_json(payload)
    else:
        print(f"success={report.success} indexed_count={report.indexed_count} database_path={services.maintenance.relative_path(target)}")
        for item in report.errors:
            print(f"- {item.platform}:{item.job_id} {item.message}")
    return 0 if report.success else 2


def _handle_summary_rebuild(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    result = services.maintenance.rebuild_summary(output_path=args.output)
    if args.json:
        payload = _base_payload("summary rebuild", workspace)
        payload.update(
            {
                "record_count": result.record_count,
                "output_path": services.maintenance.relative_path(result.output_path),
            }
        )
        _print_json(payload)
    else:
        print(f"record_count={result.record_count} output_path={services.maintenance.relative_path(result.output_path)}")
    return 0


def _handle_stale_screenings(
    args: argparse.Namespace,
    workspace: WorkspacePaths,
    services: ServiceBundle,
) -> int:
    result = services.maintenance.write_stale_screening_report(
        days=args.days,
        output_path=args.output,
    )
    path = services.maintenance.relative_path(result.output_path)
    if args.json:
        payload = _base_payload("summary stale-screenings", workspace)
        payload.update(
            {
                "status": "ok",
                "days": args.days,
                "record_count": result.record_count,
                "output_path": path,
            }
        )
        _print_json(payload)
    else:
        print(f"record_count={result.record_count} output_path={path}")
    return 0


def _handle_screening_validate(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    content = args.path.read_text(encoding="utf-8")
    valid, reason = validate_screening_structure(content)
    if args.json:
        payload = _base_payload("screening validate", workspace)
        payload.update(
            {
                "path": services.maintenance.relative_path(args.path),
                "valid": valid,
                "reason": reason or None,
            }
        )
        _print_json(payload)
    else:
        print(f"path={services.maintenance.relative_path(args.path)} valid={valid}")
        if reason:
            print(reason)
    return 0 if valid else 2


def _handle_screening_lint(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    records_root = workspace.jobs_records_dir
    repository = JDRecordRepository(records_root)
    if args.hook:
        payload = screening_lint.hook_payload_from_stdin(sys.stdin)
        paths = (
            screening_lint.hook_target_paths(payload=payload, records_root=records_root)
            if payload is not None
            else []
        )
        findings = [finding for path in paths for finding in screening_lint.lint_path(path, repository)]
        report = screening_lint.LintReport(findings=tuple(findings), keys_checked=len(paths))
    elif args.file:
        paths = [
            ((workspace.root / Path(raw)) if not Path(raw).is_absolute() else Path(raw)).resolve()
            for raw in args.file
        ]
        findings = [finding for path in paths for finding in screening_lint.lint_path(path, repository)]
        report = screening_lint.LintReport(findings=tuple(findings), keys_checked=len(paths))
    elif args.all:
        keys = list(repository.iter_keys())
        report = screening_lint.run(keys, repository)
    else:
        print("career-jobs screening lint requires --hook, --file, or --all", file=sys.stderr)
        return 2
    if args.json:
        payload = _base_payload("screening lint", workspace)
        payload.update(
            {
                "keys_checked": report.keys_checked,
                "summary": report.summary(),
                "findings": [
                    {
                        "level": finding.level,
                        "check": finding.check,
                        "file": finding.file,
                        "detail": finding.detail,
                    }
                    for finding in report.findings
                ],
            }
        )
        _print_json(payload)
        return report.exit_code
    return screening_lint.render_report(report, stdout=sys.stdout, stderr=sys.stderr)


def _handle_screening_run(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    repository = JDRecordRepository(workspace.jobs_records_dir)
    stored = repository.get(_parse_job_key(args.job_key))
    candidate_context = (
        args.candidate_context_file.read_text(encoding="utf-8")
        if args.candidate_context_file is not None
        else load_candidate_context(workspace)
    )
    company_file = args.company_file
    if company_file is None:
        company_info = CompanyInfoService(workspace=workspace)
        company_file = company_info.find_matching_file(stored.record.company)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=company_file,
        dry_run=args.dry_run,
        repository=repository if not args.dry_run else None,
        candidate_context=candidate_context,
    )
    if args.json:
        payload = _base_payload("screening run", workspace)
        payload.update(
            {
                "job_key": args.job_key,
                "dry_run": args.dry_run,
                "provider": result.provider,
                "used_fallback": result.used_fallback,
                "fallback_reason": result.fallback_reason,
                "verdict": result.verdict,
                "screening_path": str(result.screening_path),
            }
        )
        _print_json(payload)
    else:
        print(
            f"job_key={args.job_key} verdict={result.verdict} provider={result.provider} "
            f"used_fallback={result.used_fallback} screening_path={result.screening_path}"
        )
    return 0


def _semantic_eval_error_code(exc: Exception) -> str:
    message = str(exc)
    mappings = (
        ('tracked git path', 'unsafe_output_path'),
        ('inside an allowed root', 'unsafe_output_path'),
        ('must stay inside an allowed root', 'unsafe_output_path'),
        ('target already exists', 'output_exists'),
        ('capture output path', 'unsafe_output_path'),
        ('private eval root', 'unsafe_output_path'),
        ('synthetic temp root', 'unsafe_output_path'),
        ('approved temp root', 'unsafe_output_path'),
        ('missing git sha', 'missing_git_sha'),
        ('dirty tracked state', 'dirty_tracked_state'),
        ('unsupported schema', 'invalid_input'),
    )
    for needle, code in mappings:
        if needle in message:
            return code
    return 'invalid_input'

def _handle_semantic_eval_capture(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    try:
        result = services.maintenance.semantic_eval_capture(output_path=args.output, seed=args.seed)
    except (OSError, RuntimeError, ValueError) as exc:
        if args.json:
            payload = _base_payload('semantic-eval capture', workspace)
            payload.update({'status': 'error', 'error_code': _semantic_eval_error_code(exc)})
            _print_json(payload)
            return 2
        raise
    if args.json:
        payload = _base_payload('semantic-eval capture', workspace)
        payload.update(dict(result.aggregate))
        payload['status'] = result.status
        payload['error_code'] = result.error_code
        payload['output_path'] = services.maintenance.relative_path(result.output_path)
        _print_json(payload)
    else:
        print(f"status={result.status} output_path={services.maintenance.relative_path(result.output_path)}")
    return 0


def _handle_semantic_eval_run(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    try:
        result = services.maintenance.semantic_eval_run(dataset_path=args.dataset, output_path=args.output)
    except (OSError, RuntimeError, ValueError) as exc:
        if args.json:
            payload = _base_payload('semantic-eval run', workspace)
            payload.update({'status': 'error', 'error_code': _semantic_eval_error_code(exc)})
            _print_json(payload)
            return 2
        raise
    if args.json:
        payload = _base_payload('semantic-eval run', workspace)
        payload.update(dict(result.aggregate))
        payload['status'] = result.status
        payload['error_code'] = result.error_code
        payload['output_path'] = services.maintenance.relative_path(result.output_path)
        _print_json(payload)
    else:
        print(f"status={result.status} output_path={services.maintenance.relative_path(result.output_path)}")
    return 0 if result.status == 'pass' else 2


def _handle_semantic_eval_compare(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    try:
        result = services.maintenance.semantic_eval_compare(
            dataset_path=args.dataset,
            incumbent_path=args.incumbent,
            candidate_path=args.candidate,
            output_path=args.output,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        if args.json:
            payload = _base_payload('semantic-eval compare', workspace)
            payload.update({'status': 'error', 'error_code': _semantic_eval_error_code(exc)})
            _print_json(payload)
            return 2
        raise
    if args.json:
        payload = _base_payload('semantic-eval compare', workspace)
        payload.update(dict(result.aggregate))
        payload['status'] = result.status
        payload['error_code'] = result.error_code
        if result.output_path is not None:
            payload['output_path'] = services.maintenance.relative_path(result.output_path)
        _print_json(payload)
    else:
        line = f"status={result.status}"
        if result.output_path is not None:
            line += f" output_path={services.maintenance.relative_path(result.output_path)}"
        print(line)
    return 0 if result.status == 'pass' else 2


def _handle_link_add(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    if len(args.keys) < 2:
        print("link add requires at least 2 keys", file=sys.stderr)
        return 2
    assert services.link_service is not None
    keys = [_parse_job_key(raw) for raw in args.keys]
    result = services.link_service.add_link(keys, note=args.note)
    if args.json:
        payload = _base_payload("link add", workspace)
        payload.update(result.to_dict())
        _print_json(payload)
    else:
        if result.created:
            print(f"created group {result.group_id}")
        else:
            print(f"already linked in group {result.group_id}")
        for w in result.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    return 0


def _handle_link_remove(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    assert services.link_service is not None
    key = _parse_job_key(args.key)
    result = services.link_service.remove_link(key)
    if result is None:
        if args.json:
            payload = _base_payload("link remove", workspace)
            payload["removed"] = False
            _print_json(payload)
        else:
            print("소속된 링크 그룹 없음")
        return 0
    if args.json:
        payload = _base_payload("link remove", workspace)
        payload.update(result.to_dict())
        _print_json(payload)
    else:
        if result.group_deleted:
            print(f"removed {result.removed_key}, group deleted")
        else:
            print(f"removed {result.removed_key}, {result.remaining_members} members remaining")
    return 0


def _handle_link_show(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    assert services.link_service is not None
    key = _parse_job_key(args.key)
    detail = services.link_service.show_link(key)
    if detail is None:
        if args.json:
            payload = _base_payload("link show", workspace)
            payload["found"] = False
            _print_json(payload)
        else:
            print("소속된 링크 그룹 없음")
        return 0
    if args.json:
        payload = _base_payload("link show", workspace)
        payload.update(detail.to_dict())
        _print_json(payload)
    else:
        print(f"group {detail.group_id}" + (" [inconsistent]" if detail.inconsistent else ""))
        if detail.note:
            print(f"  note: {detail.note}")
        for m in detail.members:
            status = f"app={m.application_status} posting={m.posting_status}" if m.application_status else "record not found"
            print(f"  {m.platform}:{m.job_id}  {m.company or '?'} | {m.position or '?'} | {status}")
    return 0


def _handle_link_list(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    assert services.link_service is not None
    summaries = services.link_service.list_links(inconsistent_only=args.inconsistent)
    if args.json:
        payload = _base_payload("link list", workspace)
        payload["count"] = len(summaries)
        payload["groups"] = [s.to_dict() for s in summaries]
        _print_json(payload)
    else:
        if not summaries:
            print("no link groups" + (" with inconsistent status" if args.inconsistent else ""))
        else:
            for s in summaries:
                marker = " [inconsistent]" if s.inconsistent else ""
                print(f"{s.group_id} ({s.member_count} members){marker}: {', '.join(s.member_keys)}")
    return 0


def _handle_link_sync(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    assert services.link_service is not None
    key = _parse_job_key(args.key)
    result = services.link_service.sync(key, dry_run=args.dry_run)
    if args.json:
        payload = _base_payload("link sync", workspace)
        payload.update(result.to_dict())
        payload["dry_run"] = args.dry_run
        _print_json(payload)
    else:
        prefix = "[DRY-RUN] " if args.dry_run else ""
        if not result.changes:
            print(f"{prefix}no changes needed for group {result.group_id}")
        else:
            for c in result.changes:
                parts = []
                if c.to_status is not None:
                    parts.append(f"application: {c.from_status.value if c.from_status else '?'} → {c.to_status.value}")
                if c.posting_status_change is not None:
                    parts.append(f"posting: → {c.posting_status_change.value}")
                print(f"{prefix}{c.key.platform}:{c.key.job_id} {', '.join(parts)}")
        for w in result.warnings:
            print(f"  warning: {w}", file=sys.stderr)
    return 0


def _handle_console_serve(args: argparse.Namespace, workspace: WorkspacePaths, services: ServiceBundle) -> int:
    database_path = services.maintenance.derived_dir / "search.sqlite3"
    server = create_server(
        records_root=workspace.jobs_records_dir,
        database_path=database_path,
        pipeline_service=services.pipeline,
        host=args.host,
        port=args.port,
    )
    print(f"career-jobs console serving http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
