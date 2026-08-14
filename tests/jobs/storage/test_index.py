from __future__ import annotations
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import careerkit.jobs.adapters.storage.sqlite_index as sqlite_index
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


def _index_lock_path(index_path: Path) -> Path:
    return index_path.with_name(f".{index_path.name}.lock")


def _start_lock_holder(lock_path: Path, tmp_path: Path) -> tuple[subprocess.Popen[str], Path]:
    entered = tmp_path / "lock-entered"
    release = tmp_path / "lock-release"
    script = """
import fcntl
from pathlib import Path
import sys
import time

lock_path = Path(sys.argv[1])
entered = Path(sys.argv[2])
release = Path(sys.argv[3])
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    entered.write_text("entered", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(lock_path),
            str(entered),
            str(release),
        ],
        text=True,
    )
    deadline = time.monotonic() + 5
    while not entered.exists():
        if process.poll() is not None:
            raise AssertionError(f"lock holder exited early: {process.returncode}")
        if time.monotonic() >= deadline:
            process.terminate()
            raise AssertionError("lock holder did not acquire the lock")
        time.sleep(0.01)
    return process, release


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


def test_index_distinguishes_a_set_aside_record_from_an_untouched_one(tmp_path: Path) -> None:
    # Both carry a null verdict and no document. Without the reason the console
    # cannot tell a deliberate skip from an outstanding screening.
    repository = JDRecordRepository(tmp_path / "records")
    repository.create(JobRecord("wanted", "1", "Acme", "Backend"), jd_markdown="# JD")
    repository.create(JobRecord("wanted", "2", "Acme", "Backend"), jd_markdown="# JD")
    repository.update_prescreen(JobRecord("wanted", "1", "Acme", "Backend").key, "title_exclude")
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)
    assert index.rebuild().success

    reasons = {item.job_id: item.prescreen_reason for item in index.search(limit=10).items}

    assert reasons == {"1": "title_exclude", "2": None}


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


def test_rebuild_waits_for_adjacent_lock_before_creating_snapshot(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    index_path = tmp_path / "derived" / "jd.sqlite3"
    index = JDSearchIndex(index_path, repository)
    holder, release = _start_lock_holder(_index_lock_path(index_path), tmp_path)
    reports = []
    errors = []

    def rebuild() -> None:
        try:
            reports.append(index.rebuild())
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=rebuild)
    worker.start()
    time.sleep(0.1)

    assert worker.is_alive()
    assert list(index_path.parent.glob(f".{index_path.name}.*.tmp")) == []

    release.write_text("release", encoding="utf-8")
    worker.join(timeout=5)
    holder.wait(timeout=5)

    assert errors == []
    assert len(reports) == 1
    assert reports[0].success is True
    assert holder.returncode == 0


def test_waiting_rebuild_indexes_latest_canonical_state_after_lock_release(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    seeded = _seed(repository)
    index_path = tmp_path / "derived" / "jd.sqlite3"
    index = JDSearchIndex(index_path, repository)
    holder, release = _start_lock_holder(_index_lock_path(index_path), tmp_path)
    reports = []

    worker = threading.Thread(target=lambda: reports.append(index.rebuild()))
    worker.start()
    time.sleep(0.1)
    repository.update_status(
        seeded[1].key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-08-10T10:00:00+09:00",
    )

    release.write_text("release", encoding="utf-8")
    worker.join(timeout=5)
    holder.wait(timeout=5)

    result = index.search(application_status=ApplicationStatus.INTERVIEW, limit=10)

    assert len(reports) == 1
    assert reports[0].success is True
    assert [(item.platform, item.job_id) for item in result.items] == [("remember", "1")]


def test_rebuild_holds_lock_through_publication_sync_and_next_snapshot_is_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records_root = tmp_path / "records"
    first_repository = JDRecordRepository(records_root)
    seeded = _seed(first_repository)
    second_repository = JDRecordRepository(records_root)
    index_path = tmp_path / "derived" / "jd.sqlite3"
    first_index = JDSearchIndex(index_path, first_repository)
    second_index = JDSearchIndex(index_path, second_repository)
    first_sync_entered = threading.Event()
    release_first_sync = threading.Event()
    second_lock_call_entered = threading.Event()
    second_snapshot_started = threading.Event()
    reports = []
    errors = []
    original_fsync_directory = first_index._fsync_directory
    original_iter_keys = second_repository.iter_keys
    original_flock = sqlite_index.fcntl.flock
    second_worker: threading.Thread

    def blocking_fsync_directory(path: Path) -> None:
        first_sync_entered.set()
        assert release_first_sync.wait(timeout=5)
        original_fsync_directory(path)

    def observed_iter_keys():
        second_snapshot_started.set()
        yield from original_iter_keys()

    def observed_flock(file_descriptor: int, operation: int):
        if (
            threading.current_thread() is second_worker
            and operation == sqlite_index.fcntl.LOCK_EX
        ):
            second_lock_call_entered.set()
        return original_flock(file_descriptor, operation)

    monkeypatch.setattr(first_index, "_fsync_directory", blocking_fsync_directory)
    monkeypatch.setattr(second_repository, "iter_keys", observed_iter_keys)
    monkeypatch.setattr(sqlite_index.fcntl, "flock", observed_flock)

    def rebuild(index: JDSearchIndex) -> None:
        try:
            reports.append(index.rebuild())
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_worker = threading.Thread(target=rebuild, args=(first_index,))
    second_worker = threading.Thread(target=rebuild, args=(second_index,))
    first_worker.start()
    assert first_sync_entered.wait(timeout=5)

    first_repository.update_status(
        seeded[1].key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-08-10T10:00:00+09:00",
    )
    second_worker.start()
    assert second_lock_call_entered.wait(timeout=5)
    second_waited_for_lock = not second_snapshot_started.wait(timeout=0.1)

    release_first_sync.set()
    first_worker.join(timeout=5)
    second_worker.join(timeout=5)

    assert second_waited_for_lock
    assert errors == []
    assert not first_worker.is_alive()
    assert not second_worker.is_alive()
    assert len(reports) == 2
    assert all(report.success for report in reports)
    result = second_index.search(application_status=ApplicationStatus.INTERVIEW, limit=10)
    assert [(item.platform, item.job_id) for item in result.items] == [("remember", "1")]


def test_rebuild_failure_cleans_temp_database_and_rerun_succeeds(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    record = JobRecord(platform="wanted", job_id="1", company="Acme", position="Backend")
    repository.create(record, jd_markdown="# JD")
    index_path = tmp_path / "derived" / "jd.sqlite3"
    index = JDSearchIndex(index_path, repository)
    assert index.rebuild().success
    manifest_path = tmp_path / "records" / "wanted" / "1" / "record.json"
    manifest_bytes = manifest_path.read_bytes()
    before = index_path.read_bytes()
    manifest_path.write_text("{broken")

    failed = index.rebuild()

    assert failed.success is False
    assert index_path.read_bytes() == before
    assert list(index_path.parent.glob(f".{index_path.name}.*.tmp")) == []

    manifest_path.write_bytes(manifest_bytes)

    recovered = index.rebuild()

    assert recovered.success is True
    assert list(index_path.parent.glob(f".{index_path.name}.*.tmp")) == []


def test_invalid_search_pagination_is_rejected(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    _seed(repository)
    index = JDSearchIndex(tmp_path / "derived" / "jd.sqlite3", repository)
    assert index.rebuild().success

    with pytest.raises(ValueError, match="greater than zero"):
        index.search(limit=0)
    with pytest.raises(ValueError, match="must not be negative"):
        index.search(offset=-1)
