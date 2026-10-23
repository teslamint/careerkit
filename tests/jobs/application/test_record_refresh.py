from __future__ import annotations

from pathlib import Path
from typing import Any

from careerkit.jobs.adapters.http import HttpError, HttpStatusError
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.automation import JobsExtractionStage
from careerkit.jobs.application.record_refresh import placeholder_keys, refresh_records
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    ScreeningVerdict,
)

PLACEHOLDER_KEY = JobKey("groupby", "456")
CLOSED_KEY = JobKey("groupby", "457")
HEALTHY_KEY = JobKey("groupby", "458")


class GroupByDetailClient:
    """Serves the real posting only with fetch metadata, like the live API (2026-10-16).

    IDs in ``closed`` answer 404 when the metadata is present, as closed postings do.
    """

    def __init__(self, *, closed: set[str] | None = None) -> None:
        self.closed = closed or set()

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
        error_cls: type[Exception] = HttpError,
    ) -> dict[str, Any]:
        job_id = url.rsplit("/", 1)[-1]
        if "Sec-Fetch-Mode" not in (headers or {}):
            return {
                "status": 200,
                "data": {"name": "[표시 제한] Placeholder Role", "startup": {"name": "[표시 제한] Placeholder Co"}},
            }
        if job_id in self.closed:
            raise HttpStatusError("HTTP 404", status=404)
        return {
            "status": 200,
            "data": {
                "name": f"Backend Engineer {job_id}",
                "careerType": "경력",
                "experienceRange": {"min": 4, "max": 8},
                "task": "<p>서비스 개발</p>",
                "qualification": "<p>정보 없음</p>",
                "preferred": "",
                "startup": {"name": "Group Co", "briefIntro": "B2B 데이터 플랫폼", "serviceAreas": ["SaaS"]},
            },
        }

    def request_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
        max_bytes: int | None = None,
        error_cls: type[Exception] = HttpError,
    ) -> str:
        raise AssertionError(f"unexpected text request: {url}")


def _store_placeholder(repository: JDRecordRepository, key: JobKey) -> None:
    repository.create(
        JobRecord(
            platform=key.platform,
            job_id=key.job_id,
            company="[표시 제한] Placeholder Co",
            position="[표시 제한] Placeholder Role",
            source_url=f"https://groupby.kr/positions/{key.job_id}",
        ),
        jd_markdown="# [표시 제한] Placeholder Role\n",
    )


def _stage(repository: JDRecordRepository, *, closed: set[str] | None = None) -> JobsExtractionStage:
    return JobsExtractionStage(repository=repository, http_client=GroupByDetailClient(closed=closed))


def test_placeholder_keys_selects_only_groupby_records_carrying_the_marker(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _store_placeholder(repository, PLACEHOLDER_KEY)
    repository.create(
        JobRecord(platform="groupby", job_id=HEALTHY_KEY.job_id, company="Real Co", position="Backend"),
        jd_markdown="# Backend\n",
    )
    repository.create(
        JobRecord(platform="wanted", job_id="100001", company="[표시 제한] Other", position="Backend"),
        jd_markdown="# Backend\n",
    )

    assert placeholder_keys(repository) == [PLACEHOLDER_KEY]


def test_refresh_dry_run_reports_without_writing(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _store_placeholder(repository, PLACEHOLDER_KEY)
    before = repository.get(PLACEHOLDER_KEY)

    outcomes = refresh_records([PLACEHOLDER_KEY], repository=repository, extraction=_stage(repository), apply=False)

    assert [item.outcome for item in outcomes] == ["would_refresh"]
    assert outcomes[0].company == "Group Co"
    assert repository.get(PLACEHOLDER_KEY) == before


def test_refresh_apply_replaces_content_and_clears_judgement_but_keeps_application(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _store_placeholder(repository, PLACEHOLDER_KEY)
    repository.update_screening_result(
        PLACEHOLDER_KEY,
        screening_markdown="# judged on placeholder",
        screening_verdict=ScreeningVerdict.HOLD,
        screening_provider="claude",
    )
    repository.update_prescreen(PLACEHOLDER_KEY, "title_exclude")
    repository.update_status(PLACEHOLDER_KEY, application_status=ApplicationStatus.APPLIED)

    outcomes = refresh_records([PLACEHOLDER_KEY], repository=repository, extraction=_stage(repository), apply=True)

    assert outcomes[0].outcome == "refreshed"
    assert outcomes[0].cleared_verdict == "hold"
    assert outcomes[0].cleared_prescreen == "title_exclude"
    assert outcomes[0].cleared_screening is True
    stored = repository.get(PLACEHOLDER_KEY)
    assert stored.record.company == "Group Co"
    assert stored.record.position == "Backend Engineer 456"
    assert "[표시 제한]" not in stored.jd_markdown
    assert "서비스 개발" in stored.jd_markdown
    assert stored.screening_markdown is None
    assert stored.record.screening_verdict is None
    assert stored.record.screening_provider is None
    assert stored.record.prescreen_reason is None
    assert stored.record.application_status is ApplicationStatus.APPLIED
    assert placeholder_keys(repository) == []


def test_refresh_leaves_closed_posting_untouched_and_reports_failure(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _store_placeholder(repository, PLACEHOLDER_KEY)
    _store_placeholder(repository, CLOSED_KEY)
    closed_before = repository.get(CLOSED_KEY)

    outcomes = refresh_records(
        [CLOSED_KEY, PLACEHOLDER_KEY],
        repository=repository,
        extraction=_stage(repository, closed={CLOSED_KEY.job_id}),
        apply=True,
    )

    assert [item.outcome for item in outcomes] == ["failed", "refreshed"]
    assert "groupby.kr" not in (outcomes[0].error or "")
    assert repository.get(CLOSED_KEY) == closed_before
    assert placeholder_keys(repository) == [CLOSED_KEY]


def test_refresh_reports_missing_record(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")

    outcomes = refresh_records([PLACEHOLDER_KEY], repository=repository, extraction=_stage(repository), apply=True)

    assert [(item.outcome, item.error) for item in outcomes] == [("failed", "record not found")]
