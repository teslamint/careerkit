from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import csv
import inspect
import json
import os
import resource
import shlex
import subprocess
import sys
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping, cast

from careerkit.jobs.adapters.config_files import YamlConfigFileAdapter
from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
from careerkit.jobs.adapters.semantic_eval_files import SemanticEvalFileStore
from careerkit.jobs.adapters.semantic_filter import DEFAULT_MODEL, MODEL_THRESHOLDS, SemanticFilterAdapter
from careerkit.jobs.adapters.status_probes import (
    RECHECK_SAFE_PLATFORMS,
    SUPPORTED_PLATFORMS,
    ProbeOutcome,
    probe_posting_status,
)
from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    StoredJobMetadata,
    StoredJobRecord,
)
from careerkit.jobs.adapters.storage.sqlite_index import JDSearchIndex, IndexRebuildReport
from careerkit.jobs.adapters.platforms.groupby import GroupByAdapter
from careerkit.jobs.adapters.platforms.remember import RememberAdapter
from careerkit.jobs.adapters.platforms.saramin import SaraminAdapter
from careerkit.jobs.adapters.platforms.thevc import TheVCAdapter
from careerkit.jobs.adapters.platforms.wanted import WantedAdapter
from careerkit.jobs.application.config import ConfigApplyResult, ConfigCheckResult, ConfigPreviewResult, SearchConfigService, load_runtime_config
from careerkit.jobs.application.company_info import CompanyInfoService, CompanyValidationSummary
from careerkit.jobs.application.preflight import StoragePreflightResult, WorkspacePreflightService
from careerkit.jobs.application.search import SearchAdapter, SearchResult, SearchService, SearchState
from careerkit.jobs.application.semantic_eval import (
    COMPARISON_SCHEMA,
    REPORT_SCHEMA,
    SemanticComparisonReport,
    SemanticEvalCaptureSink,
    SemanticEvalReport,
    SemanticModelProvenance,
    aggregate_report_view,
    compare_reports,
    load_comparison_report_payload,
    load_dataset_payload,
    load_eval_report_payload,
    evaluate_dataset,
)

from careerkit.jobs.domain.model import ApplicationStatus, JobKey, PostingStatus, ScreeningVerdict
from careerkit.jobs.domain.naming import normalize_company_name
from careerkit.workspace import WorkspacePaths


@dataclass(frozen=True)
class SummaryRebuildResult:
    output_path: Path
    record_count: int


@dataclass(frozen=True)
class SearchStatusResult:
    last_run: str | None
    total_searches: int
    total_new_found: int
    tracked_job_keys: int


@dataclass(frozen=True)
class ClosedBackfillResult:
    keys: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class CheckClosedResult:
    closed_keys: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    skipped_platform_counts: dict[str, int]
    tripped_platforms: tuple[str, ...]
    changed: bool
    reopened_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class StaleScreeningReport:
    output_path: Path
    record_count: int


@dataclass(frozen=True)
class SemanticEvalCaptureResult:
    output_path: Path
    status: str
    error_code: str | None
    aggregate: Mapping[str, object]


@dataclass(frozen=True)
class SemanticEvalRunResult:
    output_path: Path
    status: str
    error_code: str | None
    aggregate: Mapping[str, object]


@dataclass(frozen=True)
class SemanticEvalCompareResult:
    output_path: Path | None
    status: str
    error_code: str | None
    aggregate: Mapping[str, object]


class _SemanticScorerWithOverrides:
    def __init__(self, scorer: Any, *, git_sha: str, command: str) -> None:
        self._scorer = scorer
        self._git_sha = git_sha
        self._command = command

    def prepare(self) -> None:
        self._scorer.prepare()

    def score_title(self, title: str) -> Any:
        return self._scorer.score_title(title)

    def provenance(self) -> SemanticModelProvenance:
        provenance = self._scorer.provenance()
        return SemanticModelProvenance(
            model_name=provenance.model_name,
            model_revision=provenance.model_revision,
            sentence_transformers_version=provenance.sentence_transformers_version,
            anchor_digest=provenance.anchor_digest,
            keyword_override_digest=provenance.keyword_override_digest,
            dataset_digest=provenance.dataset_digest,
            split_digest=provenance.split_digest,
            family_lock_digest=provenance.family_lock_digest,
            git_sha=self._git_sha,
            command=self._command,
            score_contract_digest=provenance.score_contract_digest,
            family_aggregation_contract_digest=provenance.family_aggregation_contract_digest,
            dataset_schema=provenance.dataset_schema,
            dataset_version=provenance.dataset_version,
        )

    def close(self) -> None:
        self._scorer.close()


_CLOSED_MARKERS = (
    "채용 마감",
    "채용이 마감",
    "마감되었습니다",
    "이 공고는 마감",
    "지원 기간이 종료",
    "상시채용 종료",
    "Position closed",
    "이 포지션은 마감",
)


class JobsMaintenanceService:
    def __init__(self, *, workspace: WorkspacePaths, http: HttpClient | None = None) -> None:
        self.workspace = workspace
        self._http = http or UrllibHttpClient()
        self.config_path = workspace.jobs_config_dir / "search_config.yaml"
        self.records_root = workspace.jobs_records_dir
        self.runtime_dir = workspace.jobs_runtime_dir
        self.derived_dir = workspace.jobs_derived_dir
        self.repository = JDRecordRepository(self.records_root)
        self.config_service = SearchConfigService(YamlConfigFileAdapter(self.config_path))
        self.company_info = CompanyInfoService(workspace=workspace)
        self.preflight = WorkspacePreflightService(
            config_service=self.config_service,
            repository=self.repository,
            derived_root=self.derived_dir,
            temp_root=self.workspace.cache_dir / "preflight",
        )

    def config_check(self) -> ConfigCheckResult:
        return self.preflight.check_config()

    def config_preview(self) -> ConfigPreviewResult:
        return self.config_service.preview()

    def config_apply(self) -> ConfigApplyResult:
        return self.config_service.apply()

    def company_validate(self, *, file_name: str | None = None, fix: bool = False) -> CompanyValidationSummary:
        return self.company_info.validate(file_name=file_name, fix=fix)

    def storage_preflight(self) -> StoragePreflightResult:
        return self.preflight.preflight_storage()

    def cleanup_preflight(self, output_root: Path) -> None:
        self.preflight.cleanup_isolated_output(output_root)

    def search(
        self,
        *,
        queries: tuple[str, ...] | list[str] | None = None,
        max_urls: int | None = None,
    ) -> SearchResult:
        raw = self.config_service.adapter.read()
        config = load_runtime_config(raw)
        if queries:
            config = replace(config, search_queries=tuple(queries))
        if max_urls is not None:
            config = replace(
                config,
                execution={**config.execution, "max_urls_per_run": max_urls},
            )
        semantic_model = str(config.semantic_filter.get("model") or DEFAULT_MODEL)
        if semantic_model.startswith("~"):
            semantic_model = str(Path(semantic_model).expanduser())
        semantic_threshold = float(
            config.semantic_filter.get(
                "threshold",
                MODEL_THRESHOLDS.get(semantic_model, MODEL_THRESHOLDS[DEFAULT_MODEL]),
            )
        )
        semantic_revision = config.semantic_filter.get("revision")
        semantic_adapter = SemanticFilterAdapter(
            self.workspace,
            model_name=semantic_model,
            threshold=semantic_threshold,
            model_revision=str(semantic_revision) if semantic_revision else None,
        )
        capability = semantic_adapter.capability(enabled=config.semantic_enabled)
        rejected_companies = {
            normalize_company_name(item.record.company)
            for item in self.repository.list_metadata()
            if item.record.application_status is ApplicationStatus.REJECTED
        }
        config = replace(config, rejected_companies=rejected_companies)
        adapters = cast(
            dict[str, SearchAdapter],
            {
                "wanted": WantedAdapter(),
                "remember": RememberAdapter(),
                "groupby": GroupByAdapter(),
                "saramin": SaraminAdapter(),
                "thevc": TheVCAdapter(),
            },
        )
        service = SearchService(
            adapters=adapters,
            semantic_filter=(
                semantic_adapter
                if config.semantic_enabled and capability.available
                else None
            ),
            semantic_capability={"available": capability.available, "reason": capability.reason},
            existing_record_checker=self._record_exists,
        )
        seen_job_keys = self._load_seen_job_keys()
        return service.run(config, SearchState(seen_job_keys=seen_job_keys))

    def persist_seen_job_keys(
        self,
        seen_job_keys: set[str],
        *,
        new_count: int | None = None,
    ) -> None:
        state = self._load_search_state()
        existing_keys = self._state_seen_job_keys(state)
        merged = sorted(existing_keys | seen_job_keys)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.seen_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "last_run": datetime.now().isoformat(),
                "seen_job_keys": merged,
                "total_searches": self._state_int(state, "total_searches") + 1,
                "total_new_found": self._state_int(state, "total_new_found")
                + (len(seen_job_keys) if new_count is None else new_count),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.seen_state_path.parent,
            prefix=f".{self.seen_state_path.name}.",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.seen_state_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def search_status(self) -> SearchStatusResult:
        state = self._load_search_state()
        last_run = state.get("last_run")
        return SearchStatusResult(
            last_run=last_run if isinstance(last_run, str) and last_run else None,
            total_searches=self._state_int(state, "total_searches"),
            total_new_found=self._state_int(state, "total_new_found"),
            tracked_job_keys=len(self._state_seen_job_keys(state)),
        )

    def reset_search_state(self) -> bool:
        existed = self.seen_state_path.exists()
        self.seen_state_path.unlink(missing_ok=True)
        return existed

    def rebuild_index(self, *, database_path: Path | None = None) -> IndexRebuildReport:
        target = database_path or (self.derived_dir / "search.sqlite3")
        return JDSearchIndex(target, self.repository).rebuild()

    def semantic_eval_capture(
        self,
        *,
        output_path: Path,
        seed: int | None = None,
    ) -> SemanticEvalCaptureResult:
        capture_root = self._semantic_capture_root()
        self._ensure_private_root(capture_root)
        validated_output = self._validate_semantic_capture_output_path(output_path)
        sink = SemanticEvalCaptureSink(
            output_path=validated_output,
            allowed_roots=(capture_root,),
            seed=0 if seed is None else seed,
            file_store=SemanticEvalFileStore(allowed_roots=(capture_root,)),
        )
        config, service = self._semantic_search_service(capture_sink=sink)
        service.run(config, SearchState(seen_job_keys=self._load_seen_job_keys()))
        written = sink.publish()
        aggregate = {
            'status': 'ok',
            'error_code': None,
            'counts': {'captured_cases': len(sink.build_payload().cases)},
            'capture_provenance': {'platforms': dict(sink.source_outcomes)},
        }
        return SemanticEvalCaptureResult(
            output_path=written,
            status='ok',
            error_code=None,
            aggregate=aggregate,
        )

    def semantic_eval_run(
        self,
        *,
        dataset_path: Path,
        output_path: Path,
    ) -> SemanticEvalRunResult:
        validated_dataset_path = self._validate_semantic_dataset_path(dataset_path)
        dataset_store = SemanticEvalFileStore(allowed_roots=self._semantic_store_roots(validated_dataset_path))
        dataset = load_dataset_payload(dataset_store.read_json(validated_dataset_path, purpose='semantic dataset'))
        self._validate_loaded_semantic_dataset_path(validated_dataset_path, dataset=dataset)
        validated_output_path = self._validate_semantic_report_output_path(output_path, dataset=dataset)
        store = SemanticEvalFileStore(allowed_roots=self._semantic_store_roots(validated_dataset_path, validated_output_path))
        if dataset.evidence_tier == 'private_gold_locked':
            self._ensure_private_root(self._semantic_private_eval_root())
            self._ensure_private_gold_workspace_clean(validated_dataset_path)
        scorer = _SemanticScorerWithOverrides(
            self._build_semantic_scorer(),
            git_sha=self._current_git_sha(),
            command=shlex.join(
                [
                    'career-jobs',
                    'semantic-eval',
                    'run',
                    '--dataset',
                    str(validated_dataset_path),
                    '--output',
                    str(validated_output_path),
                    '--json',
                ]
            ),
        )
        report = evaluate_dataset(dataset, scorer, self._resource_sampler, time.monotonic)
        payload = self._semantic_eval_report_payload(report)
        load_eval_report_payload(payload, dataset)
        written = store.write_new_json(validated_output_path, payload, purpose='semantic score report')
        aggregate = aggregate_report_view(report)
        return SemanticEvalRunResult(
            output_path=written,
            status=report.status,
            error_code=None if report.status == 'pass' else report.status,
            aggregate=aggregate,
        )

    def semantic_eval_compare(
        self,
        *,
        dataset_path: Path,
        incumbent_path: Path,
        candidate_path: Path,
        output_path: Path | None = None,
    ) -> SemanticEvalCompareResult:
        validated_dataset_path = self._validate_semantic_dataset_path(dataset_path)
        dataset_store = SemanticEvalFileStore(allowed_roots=self._semantic_store_roots(validated_dataset_path))
        dataset = load_dataset_payload(dataset_store.read_json(validated_dataset_path, purpose='semantic dataset'))
        self._validate_loaded_semantic_dataset_path(validated_dataset_path, dataset=dataset)
        if dataset.evidence_tier == 'private_gold_locked':
            self._ensure_private_root(self._semantic_private_eval_root())
        validated_incumbent_path = self._validate_semantic_report_input_path(incumbent_path, dataset=dataset)
        validated_candidate_path = self._validate_semantic_report_input_path(candidate_path, dataset=dataset)
        store = SemanticEvalFileStore(allowed_roots=self._semantic_store_roots(validated_dataset_path, validated_incumbent_path, validated_candidate_path, *( [self._validate_semantic_compare_output_path(output_path, dataset=dataset)] if output_path is not None else [] )))
        incumbent_payload = store.read_json(validated_incumbent_path, purpose='semantic score report')
        candidate_payload = store.read_json(validated_candidate_path, purpose='semantic score report')
        incumbent = load_eval_report_payload(incumbent_payload, dataset)
        candidate = load_eval_report_payload(candidate_payload, dataset)
        comparison = compare_reports(dataset, incumbent, candidate)
        aggregate = aggregate_report_view(comparison)
        error_code = None if comparison.status == 'pass' else str(comparison.comparison['reason'])
        written = None
        if output_path is not None:
            validated_output_path = self._validate_semantic_compare_output_path(output_path, dataset=dataset)
            payload = self._semantic_comparison_payload(
                comparison,
                incumbent_payload=incumbent_payload,
                candidate_payload=candidate_payload,
            )
            load_comparison_report_payload(
                payload,
                dataset,
                incumbent,
                candidate,
                incumbent_report_digest=self._payload_digest(incumbent_payload),
                candidate_report_digest=self._payload_digest(candidate_payload),
            )
            written = store.write_new_json(validated_output_path, payload, purpose='semantic comparison report')
        return SemanticEvalCompareResult(
            output_path=written,
            status=comparison.status,
            error_code=error_code,
            aggregate=aggregate,
        )

    def rebuild_summary(self, *, output_path: Path | None = None) -> SummaryRebuildResult:
        target = output_path or (self.derived_dir / "screening-summary.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        records = self.repository.list()
        content = _render_summary(
            records,
            collection_dates={
                item.record.key: self._collection_date(item)
                for item in records
            },
        )
        target.write_text(content, encoding="utf-8")
        return SummaryRebuildResult(output_path=target, record_count=len(records))

    def _collection_date(self, stored: StoredJobRecord) -> str:
        explicit = (
            _extract_date(stored.jd_markdown, labels=("수집일", "검색일"))
            or _extract_date(
                stored.screening_markdown or "",
                labels=("수집일", "검색일", "분석일"),
            )
        )
        if explicit is not None:
            return explicit

        source = stored.record.migration_source
        if source:
            source_path = self.workspace.root / "private" / source
            if source_path.is_file():
                return datetime.fromtimestamp(source_path.stat().st_mtime).date().isoformat()

        manifest = self.records_root / stored.record.platform / stored.record.job_id / "record.json"
        stat = manifest.stat()
        created_at = getattr(stat, "st_birthtime", None)
        return datetime.fromtimestamp(created_at).date().isoformat() if created_at is not None else "-"

    def backfill_closed(self, *, dry_run: bool = True) -> ClosedBackfillResult:
        keys = tuple(
            stored.record.key
            for stored in self.repository.list()
            if stored.record.posting_status is PostingStatus.ACTIVE
            and any(marker in stored.jd_markdown for marker in _CLOSED_MARKERS)
        )
        if not dry_run:
            for key in keys:
                self.repository.update_status(key, posting_status=PostingStatus.CLOSED)
        return ClosedBackfillResult(
            keys=tuple(f"{key.platform}:{key.job_id}" for key in keys),
            changed=bool(keys) and not dry_run,
        )

    def check_closed(
        self,
        *,
        dry_run: bool = True,
        delay: float = 1.0,
        platforms: tuple[str, ...] | None = None,
        recheck: bool = False,
        keys: tuple[JobKey, ...] | None = None,
    ) -> CheckClosedResult:
        if keys is not None and platforms is not None:
            raise ValueError("--platform and positional job keys are mutually exclusive")
        closed_keys: list[str] = []
        unknown_keys: list[str] = []
        reopened_keys: list[str] = []
        skipped_platform_counts: dict[str, int] = {}
        tripped_platforms: list[str] = []
        consecutive_unknowns: dict[str, int] = {}
        probed_once = False
        target_status = PostingStatus.CLOSED if recheck else PostingStatus.ACTIVE

        if keys is not None:
            records = []
            seen_keys: set[tuple[str, str]] = set()
            for key in keys:
                dedup_key = (key.platform, key.job_id)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                stored = self.repository.find(key)
                if stored is None:
                    unknown_keys.append(f"{key.platform}:{key.job_id}")
                    continue
                records.append(stored)
        else:
            records = self.repository.list()

        for stored in records:
            record = stored.record
            if record.posting_status is not target_status:
                continue
            if platforms is not None and record.platform not in platforms:
                continue
            key_label = f"{record.platform}:{record.job_id}"
            if record.platform in tripped_platforms:
                unknown_keys.append(key_label)
                continue
            if record.platform not in SUPPORTED_PLATFORMS:
                skipped_platform_counts[record.platform] = skipped_platform_counts.get(record.platform, 0) + 1
                continue
            if probed_once and delay:
                time.sleep(delay)
            probed_once = True
            outcome = probe_posting_status(
                record.platform,
                record.job_id,
                self._http,
                source_url=record.source_url,
            )
            if outcome is ProbeOutcome.UNKNOWN:
                unknown_keys.append(key_label)
                consecutive_unknowns[record.platform] = consecutive_unknowns.get(record.platform, 0) + 1
                if consecutive_unknowns[record.platform] >= 3:
                    tripped_platforms.append(record.platform)
                continue
            consecutive_unknowns[record.platform] = 0
            if recheck:
                if outcome is ProbeOutcome.ACTIVE:
                    if record.platform in RECHECK_SAFE_PLATFORMS:
                        reopened_keys.append(key_label)
                        if not dry_run:
                            self.repository.update_status(record.key, posting_status=PostingStatus.ACTIVE)
                    else:
                        unknown_keys.append(key_label)
                continue
            if outcome is ProbeOutcome.CLOSED:
                closed_keys.append(key_label)
                if not dry_run:
                    self.repository.update_status(record.key, posting_status=PostingStatus.CLOSED)
        return CheckClosedResult(
            closed_keys=tuple(closed_keys),
            unknown_keys=tuple(unknown_keys),
            skipped_platform_counts=skipped_platform_counts,
            tripped_platforms=tuple(tripped_platforms),
            changed=bool(closed_keys or reopened_keys) and not dry_run,
            reopened_keys=tuple(reopened_keys),
        )

    def write_stale_screening_report(
        self,
        *,
        days: int = 30,
        output_path: Path | None = None,
        now: float | None = None,
    ) -> StaleScreeningReport:
        current_time = datetime.now().timestamp() if now is None else now
        rows: list[dict[str, str | int]] = []
        for stored in self.repository.list():
            if stored.screening_markdown is None:
                continue
            manifest = self.records_root / stored.record.platform / stored.record.job_id / "record.json"
            age = int((current_time - manifest.stat().st_mtime) / 86400)
            if age < days:
                continue
            rows.append(
                {
                    "platform": stored.record.platform,
                    "job_id": stored.record.job_id,
                    "days_old": age,
                    "verdict": (
                        stored.record.screening_verdict.value
                        if stored.record.screening_verdict
                        else ""
                    ),
                }
            )
        rows.sort(key=lambda row: (-int(row["days_old"]), str(row["platform"]), str(row["job_id"])))
        target = output_path or (self.derived_dir / "stale-screening.csv")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["platform", "job_id", "days_old", "verdict"],
            )
            writer.writeheader()
            writer.writerows(rows)
        return StaleScreeningReport(output_path=target, record_count=len(rows))

    def show_record(self, key: JobKey) -> StoredJobMetadata:
        return self.repository.get_metadata(key)

    def relative_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace.root.resolve()))
        except ValueError:
            return str(path)

    def _semantic_search_service(
        self,
        *,
        capture_sink: SemanticEvalCaptureSink | None = None,
    ) -> tuple[Any, SearchService]:
        raw = self.config_service.adapter.read()
        config = load_runtime_config(raw)
        semantic_model = str(config.semantic_filter.get('model') or DEFAULT_MODEL)
        if semantic_model.startswith('~'):
            semantic_model = str(Path(semantic_model).expanduser())
        semantic_threshold = float(
            config.semantic_filter.get(
                'threshold',
                MODEL_THRESHOLDS.get(semantic_model, MODEL_THRESHOLDS[DEFAULT_MODEL]),
            )
        )
        semantic_revision = config.semantic_filter.get('revision')
        semantic_adapter = SemanticFilterAdapter(
            self.workspace,
            model_name=semantic_model,
            threshold=semantic_threshold,
            model_revision=str(semantic_revision) if semantic_revision else None,
        )
        capability = semantic_adapter.capability(enabled=config.semantic_enabled)
        adapters = cast(
            dict[str, SearchAdapter],
            {
                'wanted': WantedAdapter(),
                'remember': RememberAdapter(),
                'groupby': GroupByAdapter(),
                'saramin': SaraminAdapter(),
                'thevc': TheVCAdapter(),
            },
        )
        service = SearchService(
            adapters=adapters,
            semantic_filter=(semantic_adapter if config.semantic_enabled and capability.available else None),
            semantic_capability={'available': capability.available, 'reason': capability.reason},
            existing_record_checker=self._record_exists,
            semantic_capture_sink=capture_sink,
        )
        return config, service

    def _semantic_capture_root(self) -> Path:
        return (self.workspace.root / 'private' / 'jd' / 'runtime' / 'semantic-eval').resolve()

    def _semantic_private_eval_root(self) -> Path:
        return (self.workspace.root / 'private' / 'jd' / 'evals' / 'semantic-filter').resolve()

    def _semantic_temp_roots(self) -> tuple[Path, ...]:
        roots = [Path(tempfile.gettempdir()).resolve()]
        private_tmp = Path('/private/tmp')
        if private_tmp.exists():
            roots.append(private_tmp.resolve())
        runner_temp = os.environ.get('RUNNER_TEMP')
        if runner_temp:
            runner_candidate = Path(runner_temp).expanduser().resolve(strict=False)
            if runner_candidate.is_absolute() and runner_candidate.exists():
                roots.append(runner_candidate)
        deduped: list[Path] = []
        for root in roots:
            if root not in deduped:
                deduped.append(root)
        return tuple(deduped)

    def _semantic_store_roots(self, *paths: Path) -> tuple[Path, ...]:
        roots: list[Path] = []
        capture_root = self._semantic_capture_root()
        private_eval_root = self._semantic_private_eval_root()
        for path in paths:
            resolved = self._resolve_semantic_path(path)
            if self._path_within(resolved, capture_root):
                candidate = capture_root
            elif self._path_within(resolved, private_eval_root):
                candidate = private_eval_root
            else:
                candidate = resolved.parent if resolved.suffix else resolved
            if candidate not in roots:
                roots.append(candidate)
        return tuple(roots)

    def _resolve_semantic_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace.root / candidate
        return candidate.resolve(strict=False)

    def _path_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _ensure_private_root(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def _validate_semantic_capture_output_path(self, path: Path) -> Path:
        resolved = self._resolve_semantic_path(path)
        root = self._semantic_capture_root()
        if not self._path_within(resolved, root):
            raise ValueError('semantic eval capture output path must stay inside the capture output path root')
        return resolved

    def _validate_loaded_semantic_dataset_path(self, path: Path, *, dataset: Any) -> None:
        resolved = self._resolve_semantic_path(path)
        if dataset.evidence_tier == 'private_gold_locked' and not self._path_within(resolved, self._semantic_private_eval_root()):
            raise ValueError('private gold dataset path must stay inside the private eval root')

    def _validate_semantic_dataset_path(self, path: Path) -> Path:
        resolved = self._resolve_semantic_path(path)
        if self._path_within(resolved, self._semantic_private_eval_root()):
            return resolved
        for root in self._semantic_temp_roots():
            if self._path_within(resolved, root):
                return resolved
        raise ValueError('semantic eval dataset path must stay inside the private eval root or an approved temp root')

    def _validate_semantic_report_output_path(self, path: Path, *, dataset) -> Path:
        resolved = self._resolve_semantic_path(path)
        if dataset.evidence_tier == 'private_gold_locked':
            if not self._path_within(resolved, self._semantic_private_eval_root()):
                raise ValueError('semantic eval output path must stay inside the private eval root')
            return resolved
        for root in self._semantic_temp_roots():
            if self._path_within(resolved, root) and not self._path_within(resolved, self.workspace.root.resolve()):
                return resolved
        raise ValueError('semantic eval output path must stay inside the synthetic temp root')

    def _validate_semantic_report_input_path(self, path: Path, *, dataset) -> Path:
        resolved = self._resolve_semantic_path(path)
        if dataset.evidence_tier == 'private_gold_locked':
            if not self._path_within(resolved, self._semantic_private_eval_root()):
                raise ValueError('semantic eval input report path must stay inside the private eval root')
            return resolved
        for root in self._semantic_temp_roots():
            if self._path_within(resolved, root):
                return resolved
        if self._path_within(resolved, self._semantic_private_eval_root()):
            return resolved
        raise ValueError('semantic eval input report path must stay inside an approved temp root')

    def _validate_semantic_compare_output_path(self, path: Path, *, dataset) -> Path:
        return self._validate_semantic_report_output_path(path, dataset=dataset)

    def _build_semantic_scorer(self) -> SemanticFilterAdapter:
        raw = self.config_service.adapter.read()
        config = load_runtime_config(raw)
        semantic_model = str(config.semantic_filter.get('model') or DEFAULT_MODEL)
        if semantic_model.startswith('~'):
            semantic_model = str(Path(semantic_model).expanduser())
        semantic_threshold = float(
            config.semantic_filter.get(
                'threshold',
                MODEL_THRESHOLDS.get(semantic_model, MODEL_THRESHOLDS[DEFAULT_MODEL]),
            )
        )
        semantic_revision = config.semantic_filter.get('revision')
        return SemanticFilterAdapter(
            self.workspace,
            model_name=semantic_model,
            threshold=semantic_threshold,
            model_revision=str(semantic_revision) if semantic_revision else None,
        )

    def _resource_sampler(self) -> Mapping[str, object]:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        peak = int(usage.ru_maxrss)
        if sys.platform == 'darwin':
            return {'peak_rss_bytes': peak}
        return {'peak_rss_bytes': peak * 1024}

    def _current_git_sha(self) -> str:
        source_root = self._source_checkout_root()
        if source_root is not None:
            sha = self._git_sha_for_root(source_root)
            if sha is None:
                raise ValueError('missing git sha')
            return sha
        workspace_sha = self._git_sha_for_root(self.workspace.root)
        if workspace_sha is None:
            raise ValueError('missing git sha')
        return workspace_sha

    def _git_sha_for_root(self, root: Path) -> str | None:
        result = subprocess.run(
            ['git', '-C', str(root), 'rev-parse', 'HEAD'],
            check=False,
            capture_output=True,
            text=True,
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40}', sha):
            return sha
        return None

    def _source_checkout_root(self) -> Path | None:
        source_file = inspect.getsourcefile(type(self))
        if not source_file:
            return None
        return self._git_root_for_path(Path(source_file))

    def _git_root_for_path(self, path: Path) -> Path | None:
        candidate = path.resolve(strict=False)
        for directory in (candidate.parent, *candidate.parents):
            if (directory / '.git').exists():
                return directory
        return None

    def _ensure_private_gold_workspace_clean(self, dataset_path: Path) -> None:
        result = subprocess.run(
            ['git', '-C', str(self.workspace.root), 'status', '--porcelain', '--untracked-files=no'],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError('dirty tracked state')
        if result.stdout.strip():
            raise ValueError('dirty tracked state')

    def _semantic_eval_report_payload(self, report: SemanticEvalReport | SemanticComparisonReport) -> dict[str, object]:
        payload = dict(aggregate_report_view(report))
        payload.update(
            {
                'schema': REPORT_SCHEMA,
                'family_results': [item.__dict__ for item in report.family_results],
                'case_scores': [
                    {
                        'case_id': item.case_id,
                        'family_id': item.family_id,
                        'split': item.split,
                        'label': item.label,
                        'slices': list(item.slices),
                        'quick_filter_outcome': item.quick_filter_outcome,
                        'quick_filter_config_digest': item.quick_filter_config_digest,
                        'score': item.score.__dict__,
                    }
                    for item in report.case_scores
                ],
            }
        )
        return payload

    def _semantic_comparison_payload(
        self,
        report: SemanticComparisonReport,
        *,
        incumbent_payload: Mapping[str, object],
        candidate_payload: Mapping[str, object],
    ) -> dict[str, object]:
        payload = self._semantic_eval_report_payload(report)
        payload['schema'] = COMPARISON_SCHEMA
        payload['comparison'] = dict(report.comparison)
        payload['binding'] = {
            'dataset_digest': report.provenance.dataset_digest,
            'split_digest': report.provenance.split_digest,
            'family_lock_digest': report.provenance.family_lock_digest,
            'incumbent_report_digest': self._payload_digest(incumbent_payload),
            'candidate_report_digest': self._payload_digest(candidate_payload),
            'incumbent_provenance': dict(self._public_report_provenance(incumbent_payload)),
            'candidate_provenance': dict(self._public_report_provenance(candidate_payload)),
        }
        return payload


    def _public_report_provenance(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        provenance = payload.get('provenance')
        if not isinstance(provenance, Mapping):
            raise ValueError('provenance must be a mapping')
        return cast(Mapping[str, object], provenance)

    def _payload_digest(self, payload: Mapping[str, object]) -> str:
        material = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return __import__('hashlib').sha256(material.encode('utf-8')).hexdigest()

    def _record_exists(self, platform: str, raw_id: str) -> bool:
        try:
            return self.repository.find(JobKey(platform, raw_id)) is not None
        except ValueError:
            return False

    def _load_seen_job_keys(self) -> set[str]:
        payload = self._load_search_state()
        if isinstance(payload, Mapping):
            values = payload.get("seen_job_keys")
            if isinstance(values, list):
                return {str(item) for item in values}
        if isinstance(payload, list):
            return {str(item) for item in payload}
        return set()

    def _load_search_state(self) -> Mapping[str, object]:
        if not self.seen_state_path.exists():
            return {}
        try:
            payload = json.loads(self.seen_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(payload, Mapping):
            return payload
        if isinstance(payload, list):
            return {"seen_job_keys": payload}
        return {}

    @staticmethod
    def _state_seen_job_keys(state: Mapping[str, object]) -> set[str]:
        values = state.get("seen_job_keys")
        if not isinstance(values, list):
            return set()
        return {str(item) for item in values}

    @staticmethod
    def _state_int(state: Mapping[str, object], key: str) -> int:
        value = state.get(key, 0)
        if not isinstance(value, (int, str)):
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    @property
    def seen_state_path(self) -> Path:
        return self.runtime_dir / "search_state.json"


_GROUPS: tuple[tuple[ScreeningVerdict | None, str], ...] = (
    (ScreeningVerdict.RECOMMENDED, "🟢 지원 추천"),
    (ScreeningVerdict.HOLD, "🟡 지원 보류"),
    (ScreeningVerdict.NOT_RECOMMENDED, "🔴 지원 비추천"),
    (None, "❓ 판정 없음"),
)

_VERDICT_LABEL = {
    ScreeningVerdict.RECOMMENDED: "지원 추천",
    ScreeningVerdict.HOLD: "지원 보류",
    ScreeningVerdict.NOT_RECOMMENDED: "지원 비추천",
}


def _extract_date(markdown: str, *, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?m)(?:^\|\s*(?:{label_pattern})\s*\|\s*|^-\s*(?:\*\*)?(?:{label_pattern})(?:\*\*)?\s*:\s*)"
        r"(?P<date>\d{4}-\d{2}-\d{2})",
        markdown,
    )
    return match.group("date") if match else None


def _render_summary(
    records: list[StoredJobRecord],
    *,
    collection_dates: Mapping[JobKey, str],
) -> str:
    lines = [
        "# JD 스크리닝 결과 요약\n\n",
        f"> canonical file records에서 재생성 | 총 {len(records)}건\n\n",
    ]
    for verdict, heading in _GROUPS:
        group = [item for item in records if item.record.screening_verdict is verdict]
        if not group:
            continue
        lines.extend(
            (
                f"## {heading} ({len(group)}건)\n\n",
                "| Platform:ID | 회사 | 포지션 | 판정 | 지원 상태 | 공고 상태 | 수집일 |\n",
                "|-------------|------|--------|------|-----------|-----------|--------|\n",
            )
        )
        for stored in group:
            record = stored.record
            verdict_label = _VERDICT_LABEL[record.screening_verdict] if record.screening_verdict is not None else "-"
            lines.append(
                f"| {record.platform}:{record.job_id} | {record.company} | {record.position} | {verdict_label} | "
                f"{record.application_status.value} | {record.posting_status.value} | {collection_dates[record.key]} |\n"
            )
        lines.append("\n")
    return "".join(lines)
