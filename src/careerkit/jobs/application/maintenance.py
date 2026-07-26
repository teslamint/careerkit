from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import csv
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Mapping, cast

from careerkit.jobs.adapters.config_files import YamlConfigFileAdapter
from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
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
        semantic_adapter = SemanticFilterAdapter(
            self.workspace,
            model_name=semantic_model,
            threshold=semantic_threshold,
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
    ) -> CheckClosedResult:
        closed_keys: list[str] = []
        unknown_keys: list[str] = []
        reopened_keys: list[str] = []
        skipped_platform_counts: dict[str, int] = {}
        tripped_platforms: list[str] = []
        consecutive_unknowns: dict[str, int] = {}
        probed_once = False
        target_status = PostingStatus.CLOSED if recheck else PostingStatus.ACTIVE
        for stored in self.repository.list():
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
