from __future__ import annotations

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.adapters.storage.sqlite_index import JDSearchIndex
from careerkit.jobs.domain.model import ApplicationStatus, JobRecord, ScreeningVerdict


def test_generate_like_rebuild_is_repeatable(tmp_path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    for platform, job_id in (("wanted", "100"), ("remember", "100"), ("groupby", "200")):
        repository.create(
            JobRecord(platform=platform, job_id=job_id, company=platform, position="Backend", screening_verdict=ScreeningVerdict.HOLD),
            jd_markdown="# JD",
        )
    database = tmp_path / "derived" / "jd.sqlite3"
    index = JDSearchIndex(database, repository)

    first = index.rebuild()
    database.unlink()
    second = index.rebuild()

    assert first.success and second.success
    assert first.indexed_count == second.indexed_count == 3
    assert database.exists()


def test_closed_posting_axis_backfill_is_storage_compatible(tmp_path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    record = JobRecord(
        platform="wanted",
        job_id="88",
        company="Acme",
        position="Backend",
        application_status=ApplicationStatus.APPLIED,
    )
    repository.create(record, jd_markdown="# JD\n이 공고는 마감되었습니다")

    updated = repository.update_status(record.key, posting_status=record.posting_status.CLOSED)

    assert updated.record.posting_status.value == "closed"
    assert updated.record.application_status.value == "applied"
