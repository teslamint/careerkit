from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil
import tempfile

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, JobRecordRepositoryError
from careerkit.jobs.adapters.storage.sqlite_index import JDSearchIndex
from careerkit.jobs.application.config import ConfigCheckResult, SearchConfigService
from careerkit.jobs.domain.model import SCHEMA_VERSION


@dataclass(frozen=True)
class PreflightFinding:
    code: str
    message: str
    target: str


@dataclass(frozen=True)
class StoragePreflightResult:
    ready: bool
    record_count: int
    screening_count: int
    checked_keys: tuple[str, ...]
    schema_version: int
    isolated_output_root: Path
    findings: tuple[PreflightFinding, ...]
    status_counts: dict[str, int]
    application_timestamp_categories: dict[str, int] = field(default_factory=dict)


class WorkspacePreflightService:
    def __init__(
        self,
        *,
        config_service: SearchConfigService,
        repository: JDRecordRepository,
        derived_root: Path,
        temp_root: Path | None = None,
    ) -> None:
        self.config_service = config_service
        self.repository = repository
        self.derived_root = derived_root
        self.temp_root = temp_root

    def check_config(self) -> ConfigCheckResult:
        return self.config_service.check()

    def preflight_storage(self) -> StoragePreflightResult:
        findings: list[PreflightFinding] = []
        checked_keys: list[str] = []
        status_counts: dict[str, int] = {}
        application_timestamp_categories: dict[str, int] = {}
        screening_count = 0
        keys = tuple(self.repository.iter_keys())
        if self.temp_root is not None:
            self.temp_root.mkdir(parents=True, exist_ok=True)
        output_root = Path(
            tempfile.mkdtemp(prefix="careerkit-preflight-", dir=self.temp_root)
            if self.temp_root is not None
            else tempfile.mkdtemp(prefix="careerkit-preflight-")
        )
        rebuild_root = output_root / "derived"
        index = JDSearchIndex(rebuild_root / "jd.sqlite3", self.repository)

        for key in keys:
            checked_keys.append(f"{key.platform}:{key.job_id}")
            try:
                validated = self.repository.validate_integrity(key)
            except (JobRecordRepositoryError, OSError, UnicodeError, ValueError) as exc:
                findings.append(
                    PreflightFinding(
                        code="integrity_error",
                        message=str(exc),
                        target=f"{key.platform}:{key.job_id}",
                    )
                )
                continue
            if validated.record.schema_version != SCHEMA_VERSION:
                findings.append(
                    PreflightFinding(
                        code="schema_version_mismatch",
                        message=f"expected schema version {SCHEMA_VERSION}",
                        target=f"{key.platform}:{key.job_id}",
                    )
                )
            checked_key = validated.record.key
            if checked_key != key:
                findings.append(
                    PreflightFinding(
                        code="compound_identity_mismatch",
                        message="validated key differs from enumerated key",
                        target=f"{key.platform}:{key.job_id}",
                    )
                )
                continue
            timestamp_category = _categorize_application_timestamp(
                validated.record.application_status_updated_at
            )
            application_timestamp_categories[timestamp_category] = (
                application_timestamp_categories.get(timestamp_category, 0) + 1
            )
            verdict = validated.record.screening_verdict.value if validated.record.screening_verdict else "unscreened"
            for label in (
                f"screening:{verdict}",
                f"application:{validated.record.application_status.value}",
                f"posting:{validated.record.posting_status.value}",
            ):
                status_counts[label] = status_counts.get(label, 0) + 1
            if validated.has_screening:
                screening_count += 1

        rebuild = index.rebuild()
        if not rebuild.success:
            for error in rebuild.errors:
                findings.append(
                    PreflightFinding(
                        code="derived_rebuild_error",
                        message=error.message,
                        target=f"{error.platform}:{error.job_id}",
                    )
                )
        elif rebuild.indexed_count != len(keys):
            findings.append(
                PreflightFinding(
                    code="derived_count_mismatch",
                    message=f"expected {len(keys)} indexed rows, got {rebuild.indexed_count}",
                    target="derived/jd.sqlite3",
                )
            )

        return StoragePreflightResult(
            ready=not findings,
            record_count=len(keys),
            screening_count=screening_count,
            checked_keys=tuple(sorted(checked_keys)),
            schema_version=SCHEMA_VERSION,
            isolated_output_root=output_root,
            findings=tuple(findings),
            status_counts=dict(sorted(status_counts.items())),
            application_timestamp_categories=dict(
                sorted(application_timestamp_categories.items())
            ),
        )

    def cleanup_isolated_output(self, output_root: Path) -> None:
        shutil.rmtree(output_root, ignore_errors=True)


def _categorize_application_timestamp(value: str | None) -> str:
    if value is None:
        return "absent"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "invalid"
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return "naive"
    return "aware"
