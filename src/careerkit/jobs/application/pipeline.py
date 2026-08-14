from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, StoredJobMetadata, StoredJobRecord
from careerkit.jobs.application import status as status_app
from careerkit.jobs.application.storage_migration import extract_job_id, get_platform_from_url
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, PostingStatus, ScreeningVerdict
from careerkit.jobs.domain.verdict import classify_by_verdict, parse_verdict_from_screening, to_screening_verdict


@dataclass(frozen=True)
class IngestResult:
    source: str
    job_id: str | None
    outcome: str
    message: str
    target: str | None = None
    current: str | None = None
    verdict: str | None = None


@dataclass(frozen=True)
class QueueStatusResult:
    total: int
    counts: dict[str, int]


@dataclass(frozen=True)
class PrescreenedListing:
    set_aside: list[StoredJobMetadata]
    legacy: list[StoredJobMetadata]


class JobsPipelineService:
    def __init__(self, *, workspace_root: Path, repository: JDRecordRepository, runtime_dir: Path) -> None:
        self.workspace_root = workspace_root
        self.repository = repository
        self.runtime_dir = runtime_dir

    def ingest_url(self, url: str) -> IngestResult:
        job_id = extract_job_id(url)
        if not job_id:
            return IngestResult(source=url, job_id=None, outcome="error", message="URL에서 job_id를 추출할 수 없습니다.")
        platform = get_platform_from_url(url)
        if not platform:
            return IngestResult(source=url, job_id=job_id, outcome="error", message="지원하지 않는 플랫폼입니다.")
        existing = self.repository.find(JobKey(platform, job_id))
        if existing is not None:
            target = existing.record.screening_verdict.value if existing.record.screening_verdict else None
            return IngestResult(
                source=url,
                job_id=job_id,
                outcome="duplicate",
                message=f"이미 존재: {platform}/{job_id}",
                target=target,
            )
        return IngestResult(
            source=url,
            job_id=job_id,
            outcome="needs_manual",
            message=f"추출 필요 (플랫폼: {platform})",
        )

    def ingest_file(self, path: Path) -> list[IngestResult]:
        target = path if path.is_absolute() else self.workspace_root / path
        if not target.exists():
            raise FileNotFoundError(target)
        results: list[IngestResult] = []
        for line in target.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            results.append(self.ingest_url(stripped))
        return results

    def queue_status(self) -> QueueStatusResult:
        queue_path = self.runtime_dir / "queue" / "queue.json"
        if not queue_path.exists():
            return QueueStatusResult(total=0, counts={})
        payload = json.loads(queue_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            payload = payload["items"]
        if not isinstance(payload, list):
            raise ValueError("queue.json must contain a list or an object with an 'items' list")
        counts = Counter()
        for item in payload:
            if isinstance(item, dict):
                counts[str(item.get("status", "pending"))] += 1
            else:
                counts["pending"] += 1
        return QueueStatusResult(total=len(payload), counts=dict(sorted(counts.items())))

    def migrate_queue_status(self) -> list[dict[str, str]]:
        return status_app.migrate_status(repository=self.repository)

    def classify_record(self, key: JobKey, *, dry_run: bool = False) -> IngestResult:
        stored = self.repository.get(key)
        identity = f"{stored.record.platform}/{stored.record.job_id}"
        current_status = stored.record.application_status
        if current_status is not ApplicationStatus.PENDING:
            status_label = current_status.value
            return IngestResult(
                source=identity,
                job_id=stored.record.job_id,
                outcome="skipped",
                message=f"보호된 상태 ({status_label}): 재분류 스킵",
                current=status_label,
            )
        content = stored.screening_markdown or stored.jd_markdown
        verdict = parse_verdict_from_screening(content)
        if not verdict:
            return IngestResult(
                source=identity,
                job_id=stored.record.job_id,
                outcome="skipped",
                message="판정 결과를 찾을 수 없습니다. 스크리닝이 필요합니다.",
                current=current_status.value,
            )
        target_folder = classify_by_verdict(verdict)
        if not target_folder:
            return IngestResult(
                source=identity,
                job_id=stored.record.job_id,
                outcome="skipped",
                message=f"판정 결과를 분류할 수 없습니다: {verdict}",
                current=current_status.value,
                verdict=verdict,
            )
        if dry_run:
            return IngestResult(
                source=identity,
                job_id=stored.record.job_id,
                outcome="success",
                message=f"[DRY-RUN] {verdict} → {target_folder}",
                target=target_folder,
                current=current_status.value,
                verdict=verdict,
            )
        canonical_verdict = to_screening_verdict(verdict)
        if canonical_verdict is None:
            raise ValueError(f"Unsupported screening verdict: {verdict}")
        self.repository.update_verdict(stored.record.key, canonical_verdict)
        return IngestResult(
            source=identity,
            job_id=stored.record.job_id,
            outcome="success",
            message=f"{verdict} → {target_folder}",
            target=target_folder,
            current=current_status.value,
            verdict=verdict,
        )

    def list_prescreened(self, *, reason: str | None = None) -> PrescreenedListing:
        """List records awaiting screening, split by whether a pre-screen reason exists."""
        set_aside: list[StoredJobMetadata] = []
        legacy: list[StoredJobMetadata] = []
        for item in self.repository.list_metadata():
            record = item.record
            if record.posting_status is PostingStatus.CLOSED or item.has_screening:
                continue
            if reason is not None and record.prescreen_reason != reason:
                continue
            if record.screening_verdict is None:
                if record.prescreen_reason is not None:
                    set_aside.append(item)
            else:
                legacy.append(item)
        return PrescreenedListing(set_aside=set_aside, legacy=legacy)

    def show_record(self, key: JobKey) -> StoredJobMetadata:
        return self.repository.get_metadata(key)

    def set_record_status(
        self,
        key: JobKey,
        *,
        application_status: ApplicationStatus | None = None,
        posting_status: PostingStatus | None = None,
        application_status_updated_at: str | None = None,
        application_note: str | None = None,
    ) -> StoredJobRecord:
        return self.repository.update_status(
            key,
            application_status=application_status,
            posting_status=posting_status,
            application_status_updated_at=application_status_updated_at,
            application_note=application_note,
        )

    def set_record_verdict(self, key: JobKey, verdict: ScreeningVerdict) -> StoredJobRecord:
        return self.repository.update_verdict(key, verdict)

    def storage_status(self) -> dict[str, int]:
        return status_app.get_status(repository=self.repository)
