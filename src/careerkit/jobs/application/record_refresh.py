"""Re-extract canonical records whose stored JD content is wrong.

``JDRecordRepository.save`` keeps screening output when a record already exists, so
re-running extraction cannot repair a record stored from bad content. This module
replaces the content and clears what was judged against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from careerkit.jobs.adapters.http import HttpError
from careerkit.jobs.adapters.platforms.groupby import GROUPBY_PLACEHOLDER_MARKER
from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordNotFound,
    StoredJobRecord,
)
from careerkit.jobs.domain.model import JobKey


class RecordPreviewer(Protocol):
    def preview(self, url: str) -> StoredJobRecord: ...


@dataclass(frozen=True)
class RefreshOutcome:
    job_key: str
    outcome: str
    company: str | None = None
    position: str | None = None
    cleared_verdict: str | None = None
    cleared_prescreen: str | None = None
    cleared_screening: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "job_key": self.job_key,
            "outcome": self.outcome,
            "company": self.company,
            "position": self.position,
            "cleared_verdict": self.cleared_verdict,
            "cleared_prescreen": self.cleared_prescreen,
            "cleared_screening": self.cleared_screening,
            "error": self.error,
        }


def placeholder_keys(repository: JDRecordRepository) -> list[JobKey]:
    """GroupBy records stored from the placeholder posting the API serves to non-browsers."""
    keys: list[JobKey] = []
    for key in repository.iter_keys():
        if key.platform != "groupby":
            continue
        stored = repository.get(key)
        fields = (stored.record.company, stored.record.position, stored.jd_markdown)
        if any(GROUPBY_PLACEHOLDER_MARKER in value for value in fields):
            keys.append(key)
    return keys


def refresh_records(
    keys: Iterable[JobKey],
    *,
    repository: JDRecordRepository,
    extraction: RecordPreviewer,
    apply: bool,
) -> list[RefreshOutcome]:
    """Re-extract each record from its source URL; replace it only when ``apply`` is set.

    A record whose extraction fails stays untouched and is reported as ``failed``.
    """
    outcomes: list[RefreshOutcome] = []
    for key in keys:
        job_key = f"{key.platform}:{key.job_id}"
        try:
            current = repository.get(key)
        except JobRecordNotFound:
            outcomes.append(RefreshOutcome(job_key=job_key, outcome="failed", error="record not found"))
            continue
        url = current.record.source_url
        if not url:
            outcomes.append(RefreshOutcome(job_key=job_key, outcome="failed", error="record has no source_url"))
            continue
        try:
            fresh = extraction.preview(url)
        except (HttpError, OSError, RuntimeError, ValueError) as exc:
            outcomes.append(
                RefreshOutcome(job_key=job_key, outcome="failed", error=str(exc).replace(url, job_key))
            )
            continue
        if fresh.record.key != key:
            outcomes.append(
                RefreshOutcome(job_key=job_key, outcome="failed", error="source_url resolves to a different record")
            )
            continue
        verdict = current.record.screening_verdict
        cleared = RefreshOutcome(
            job_key=job_key,
            outcome="refreshed" if apply else "would_refresh",
            company=fresh.record.company,
            position=fresh.record.position,
            cleared_verdict=verdict.value if verdict is not None else None,
            cleared_prescreen=current.record.prescreen_reason,
            cleared_screening=current.screening_markdown is not None,
        )
        if apply:
            repository.replace_jd(
                key,
                company=fresh.record.company,
                position=fresh.record.position,
                source_url=url,
                jd_markdown=fresh.jd_markdown,
            )
        outcomes.append(cleared)
    return outcomes
