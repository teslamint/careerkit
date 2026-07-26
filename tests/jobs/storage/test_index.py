from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.sqlite_index import JDSearchIndex
from careerkit.jobs.domain.model import ApplicationStatus, JobRecord, PostingStatus, ScreeningVerdict
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository


def _seed(repository: JDRecordRepository) -> list[JobRecord]:
    records = [
        JobRecord("wanted", "1", "Acme", "Backend", screening_verdict=ScreeningVerdict.RECOMMENDED),
        JobRecord("remember", "1", "Beta", "Platform", application_status=ApplicationStatus.APPLIED),
        JobRecord("groupby", "2", "Gamma", "Infra", posting_status=PostingStatus.CLOSED),
    ]
    for item in records:
        repository.create(item, jd_markdown="# JD")
    repository.update_screening_result(records[0].key, screening_markdown="# Screening")
    return records


def test_rebuild_and_search_keep_platform_local_identity(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)

    report = index.rebuild()
    result = index.search(limit=10)

    assert report.success
    assert report.indexed_count == 3
    assert {(item.platform, item.job_id) for item in result.items} == {
        ("wanted", "1"),
        ("remember", "1"),
        ("groupby", "2"),
    }


def test_combined_platform_and_application_status_filters_use_and_semantics(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)
    assert index.rebuild().success

    result = index.search(platform="remember", application_status=ApplicationStatus.APPLIED, limit=10)

    assert [(item.platform, item.job_id) for item in result.items] == [("remember", "1")]
    assert result.total == 1


def test_null_and_each_screening_verdict_are_independently_filterable(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    repository.create(JobRecord("wanted", "3", "Delta", "Ops", screening_verdict=ScreeningVerdict.HOLD), jd_markdown="# JD")
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)
    assert index.rebuild().success

    unscreened = index.search(screening_verdict="null", limit=10)
    hold = index.search(screening_verdict=ScreeningVerdict.HOLD, limit=10)

    assert {(item.platform, item.job_id) for item in unscreened.items} == {("remember", "1"), ("groupby", "2")}
    assert {(item.platform, item.job_id) for item in hold.items} == {("wanted", "3")}


def test_rebuild_failure_preserves_previous_index_bytes(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    record = JobRecord(platform="wanted", job_id="1", company="Acme", position="Backend")
    repository.create(record, jd_markdown="# JD")
    index_path = tmp_path / "derived" / "jd.sqlite3"
    index = JDSearchIndex(index_path, repository)
    assert index.rebuild().success
    before = index_path.read_bytes()
    (tmp_path / "records" / "wanted" / "1" / "record.json").write_text("{broken")

    report = index.rebuild()

    assert not report.success
    assert index_path.read_bytes() == before


def test_invalid_search_pagination_is_rejected(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)
    assert index.rebuild().success

    with pytest.raises(ValueError, match="greater than zero"):
        index.search(limit=0)
    with pytest.raises(ValueError, match="must not be negative"):
        index.search(offset=-1)
