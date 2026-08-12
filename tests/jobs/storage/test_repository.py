from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import threading
from typing import Any

import pytest

import careerkit.jobs.adapters.storage.file_records as file_records
from careerkit.jobs.domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)
from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordIntegrityError,
    JobRecordNotFound,
)


def _record(
    *,
    platform: str = "wanted",
    job_id: str = "100001",
    company: str = "Example",
    position: str = "Backend Engineer",
) -> JobRecord:
    return JobRecord(
        platform=platform,
        job_id=job_id,
        company=company,
        position=position,
        source_url=f"https://example.com/{platform}/{job_id}",
    )


def _append_two_application_events(
    repo: JDRecordRepository,
    key: JobKey,
) -> tuple[ApplicationEvent, ApplicationEvent]:
    events = (
        ApplicationEvent(
            status=ApplicationStatus.APPLIED,
            occurred_at="2026-07-13T09:00:00+09:00",
            note="지원서 제출",
        ),
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-07-14T11:00:00+09:00",
            note="1차 기술 면접",
        ),
    )
    for event in events:
        repo.update_status(
            key,
            application_status=event.status,
            application_status_updated_at=event.occurred_at,
            application_note=event.note,
        )
    return events


def test_save_and_get_jd_only_record(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    saved = repo.create(_record(), jd_markdown="# JD\nbody")

    fetched = repo.get(JobKey("wanted", "100001"))

    assert fetched == saved
    assert fetched.record.key == JobKey("wanted", "100001")
    assert fetched.jd_markdown == "# JD\nbody"
    assert fetched.screening_markdown is None


def test_add_screening_creates_new_revision_without_mutating_old_content(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="# JD\nbody")
    first_manifest = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    updated = repo.update_screening_result(JobKey("wanted", "100001"), screening_markdown="# Screening\nfit")
    second_manifest = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    assert updated.screening_markdown == "# Screening\nfit"
    assert first_manifest["content"]["revision"] != second_manifest["content"]["revision"]
    assert (tmp_path / "wanted" / "100001" / second_manifest["content"]["jd_path"]).read_text() == "# JD\nbody"
    assert (tmp_path / "wanted" / "100001" / second_manifest["content"]["screening_path"]).read_text() == "# Screening\nfit"


def test_manifest_persists_compound_identity_and_content_hashes(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(platform="wanted", job_id="42"), jd_markdown="# JD\nbody")
    repo.update_screening_result(JobKey("wanted", "42"), screening_markdown="# Screening\nfit")

    manifest_path = tmp_path / "wanted" / "42" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    record_dir = manifest_path.parent
    jd_path = record_dir / manifest["content"]["jd_path"]
    screening_path = record_dir / manifest["content"]["screening_path"]

    assert manifest["record"]["platform"] == "wanted"
    assert manifest["record"]["job_id"] == "42"
    assert manifest["content"]["revision"]
    assert manifest["content"]["jd_sha256"] == hashlib.sha256(jd_path.read_bytes()).hexdigest()
    assert manifest["content"]["screening_sha256"] == hashlib.sha256(screening_path.read_bytes()).hexdigest()


def test_same_numeric_id_is_isolated_per_platform(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(platform="wanted", job_id="42"), jd_markdown="wanted jd")
    repo.create(_record(platform="remember", job_id="42"), jd_markdown="remember jd")

    keys = [item.record.key for item in repo.list()]

    assert keys == [JobKey("remember", "42"), JobKey("wanted", "42")]
    assert repo.get(JobKey("wanted", "42")).jd_markdown == "wanted jd"
    assert repo.get(JobKey("remember", "42")).jd_markdown == "remember jd"


def test_traversal_rejection_happens_before_any_write(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)

    with pytest.raises(ValueError, match="Invalid platform"):
        repo.create(_record(platform="../wanted"), jd_markdown="bad")

    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    "mutator, expected",
    [
        ("missing_manifest", "Manifest not found"),
        ("corrupt_manifest", "Invalid manifest"),
        ("hash_mismatch", "Hash mismatch"),
    ],
)
def test_get_rejects_missing_or_corrupt_storage(tmp_path: Path, mutator: str, expected: str) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    record_dir = tmp_path / "wanted" / "100001"

    if mutator == "missing_manifest":
        (record_dir / "record.json").unlink()
    elif mutator == "corrupt_manifest":
        (record_dir / "record.json").write_text("not-json")
    else:
        manifest = json.loads((record_dir / "record.json").read_text())
        jd_path = record_dir / manifest["content"]["jd_path"]
        jd_path.write_text("tampered")

    with pytest.raises(JobRecordIntegrityError, match=expected):
        repo.get(JobKey("wanted", "100001"))


def test_status_update_changes_axes_without_rewriting_content(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    before = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    updated = repo.update_status(
        JobKey("wanted", "100001"),
        application_status=ApplicationStatus.APPLIED,
        posting_status=PostingStatus.CLOSED,
        application_status_updated_at="2026-07-13T12:00:00+09:00",
    )
    after = json.loads((tmp_path / "wanted" / "100001" / "record.json").read_text())

    assert updated.record.application_status is ApplicationStatus.APPLIED
    assert updated.record.posting_status is PostingStatus.CLOSED
    assert updated.record.application_status_updated_at == "2026-07-13T12:00:00+09:00"
    assert before["content"]["revision"] == after["content"]["revision"]


def test_status_update_without_timestamp_appends_timezone_aware_event(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")

    updated = repo.update_status(
        JobKey("wanted", "100001"),
        application_status=ApplicationStatus.APPLIED,
    )

    assert len(updated.record.application_history) == 1
    event = updated.record.application_history[0]
    assert event.status is ApplicationStatus.APPLIED
    assert event.note is None
    assert datetime.fromisoformat(event.occurred_at).tzinfo is not None
    assert updated.record.application_status_updated_at == event.occurred_at


def test_same_status_updates_append_distinct_events(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")

    first = repo.update_status(
        key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-07-13T09:00:00+09:00",
        application_note="1차 기술 면접",
    )
    second = repo.update_status(
        key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-07-14T11:00:00+09:00",
        application_note="2차 기술 면접",
    )

    assert first.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-07-13T09:00:00+09:00",
            note="1차 기술 면접",
        ),
    )
    assert second.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-07-13T09:00:00+09:00",
            note="1차 기술 면접",
        ),
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-07-14T11:00:00+09:00",
            note="2차 기술 면접",
        ),
    )
    assert second.record.application_status is ApplicationStatus.INTERVIEW
    assert second.record.application_status_updated_at == "2026-07-14T11:00:00+09:00"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"application_note": "지원서 제출"}, "application status"),
        (
            {"application_status_updated_at": "2026-07-13T09:00:00+09:00"},
            "application status",
        ),
    ],
)
def test_status_update_rejects_note_or_timestamp_without_application_status(
    tmp_path: Path,
    kwargs: dict[str, Any],
    error: str,
) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match=error):
        repo.update_status(key, **kwargs)

    assert manifest_path.read_bytes() == before


def test_status_update_rejects_empty_timestamp_without_mutation(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="invalid ISO 8601 timestamp"):
        repo.update_status(
            key,
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="",
        )

    assert manifest_path.read_bytes() == before


def test_posting_only_update_does_not_append_application_event(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")

    updated = repo.update_status(key, posting_status=PostingStatus.CLOSED)

    assert updated.record.posting_status is PostingStatus.CLOSED
    assert updated.record.application_history == ()
    assert updated.record.application_status is ApplicationStatus.PENDING


def test_corrective_status_update_preserves_earlier_events(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")

    updated = repo.update_status(
        key,
        application_status=ApplicationStatus.APPLIED,
        application_status_updated_at="2026-07-13T09:00:00+09:00",
        application_note="잘못 선택함",
    )
    corrected = repo.update_status(
        key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-07-13T12:00:00+09:00",
        application_note="실제 상태로 정정",
    )

    assert updated.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.APPLIED,
            occurred_at="2026-07-13T09:00:00+09:00",
            note="잘못 선택함",
        ),
    )
    assert corrected.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.APPLIED,
            occurred_at="2026-07-13T09:00:00+09:00",
            note="잘못 선택함",
        ),
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-07-13T12:00:00+09:00",
            note="실제 상태로 정정",
        ),
    )


def test_save_merges_refresh_fields_into_latest_locked_record(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="# old")
    stale = repo.get(key)
    expected_history = _append_two_application_events(repo, key)

    refreshed = repo.save(
        replace(
            stale.record,
            company="Updated Company",
            position="Updated Position",
            source_url="https://example.com/refreshed",
        ),
        jd_markdown="# new",
    )

    assert refreshed.record.company == "Updated Company"
    assert refreshed.record.position == "Updated Position"
    assert refreshed.record.source_url == "https://example.com/refreshed"
    assert refreshed.record.application_status is ApplicationStatus.INTERVIEW
    assert refreshed.record.application_history == expected_history
    assert refreshed.record.prescreen_reason is None
    assert refreshed.jd_markdown == "# new"


def test_save_from_stale_refresh_only_updates_allowed_record_fields(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="# old")
    stale = repo.get(key)
    repo.update_verdict(key, ScreeningVerdict.HOLD, prescreen_reason="keep me")

    refreshed = repo.save(
        replace(
            stale.record,
            company="Updated Company",
            position="Updated Position",
            source_url="https://example.com/refreshed",
            screening_verdict=ScreeningVerdict.RECOMMENDED,
            prescreen_reason="drop me",
        ),
        jd_markdown="# new",
    )

    assert refreshed.record.company == "Updated Company"
    assert refreshed.record.position == "Updated Position"
    assert refreshed.record.source_url == "https://example.com/refreshed"
    assert refreshed.record.screening_verdict is ScreeningVerdict.HOLD
    assert refreshed.record.prescreen_reason == "keep me"


def test_v2_record_with_invalid_legacy_current_timestamp_can_be_refreshed_without_history_loss(
    tmp_path: Path,
) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(
        JobRecord(
            "wanted",
            "legacy-invalid",
            "Acme",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="not-a-timestamp",
        ),
        jd_markdown="# JD",
    )
    key = JobKey("wanted", "legacy-invalid")
    manifest_path = tmp_path / "wanted" / "legacy-invalid" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record"]["application_history"] = []
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    stale = repo.get(key)
    refreshed = repo.save(
        replace(stale.record, company="Updated Company"),
        jd_markdown="# new",
    )

    assert refreshed.record.company == "Updated Company"
    assert refreshed.record.application_history == ()
    assert refreshed.record.application_status_updated_at == "not-a-timestamp"


def test_valid_application_event_replaces_invalid_legacy_current_timestamp(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(
        JobRecord(
            "wanted",
            "legacy-invalid",
            "Acme",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="not-a-timestamp",
        ),
        jd_markdown="# JD",
    )
    key = JobKey("wanted", "legacy-invalid")
    manifest_path = tmp_path / "wanted" / "legacy-invalid" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record"]["application_history"] = []
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    updated = repo.update_status(
        key,
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-08-10T09:30:00+09:00",
        application_note="1차 기술 면접",
    )

    assert updated.record.application_status is ApplicationStatus.INTERVIEW
    assert updated.record.application_status_updated_at == "2026-08-10T09:30:00+09:00"
    assert updated.record.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.INTERVIEW,
            occurred_at="2026-08-10T09:30:00+09:00",
            note="1차 기술 면접",
        ),
    )


def test_verdict_update_preserves_newer_status_metadata(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    repo.update_status(JobKey("wanted", "100001"), application_status=ApplicationStatus.APPLIED)

    updated = repo.update_verdict(JobKey("wanted", "100001"), ScreeningVerdict.RECOMMENDED)

    assert updated.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert updated.record.application_status is ApplicationStatus.APPLIED


def test_verdict_update_preserves_application_history(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    expected_history = _append_two_application_events(repo, key)

    updated = repo.update_verdict(key, ScreeningVerdict.RECOMMENDED)

    assert updated.record.application_history == expected_history


def test_screening_result_atomically_preserves_newer_status_metadata(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    repo.update_status(JobKey("wanted", "100001"), posting_status=PostingStatus.CLOSED)

    updated = repo.update_screening_result(
        JobKey("wanted", "100001"),
        screening_markdown="# screening",
        screening_verdict=ScreeningVerdict.HOLD,
    )

    assert updated.record.screening_verdict is ScreeningVerdict.HOLD
    assert updated.record.posting_status is PostingStatus.CLOSED
    assert updated.screening_markdown == "# screening"


def test_screening_result_preserves_application_history(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    expected_history = _append_two_application_events(repo, key)

    updated = repo.update_screening_result(
        key,
        screening_markdown="# screening",
        screening_verdict=ScreeningVerdict.HOLD,
    )

    assert updated.record.application_history == expected_history
    assert updated.screening_markdown == "# screening"


def test_stale_refresh_waits_for_status_publish_and_preserves_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_repo = JDRecordRepository(tmp_path)
    refresh_repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    status_repo.create(_record(), jd_markdown="# old")
    stale = refresh_repo.get(key)
    publish_entered = threading.Event()
    publish_release = threading.Event()
    refresh_lock_call_entered = threading.Event()
    refresh_finished = threading.Event()
    errors: list[Exception] = []
    original_publish = status_repo._publish_manifest
    original_flock = file_records.fcntl.flock
    refresh_worker: threading.Thread

    def observed_flock(file_descriptor: int, operation: int) -> Any:
        if (
            threading.current_thread() is refresh_worker
            and operation == file_records.fcntl.LOCK_EX
        ):
            refresh_lock_call_entered.set()
        return original_flock(file_descriptor, operation)

    def blocking_publish(record_dir: Path, record: JobRecord, content: Any) -> None:
        publish_entered.set()
        assert publish_release.wait(timeout=5)
        original_publish(record_dir, record, content)

    monkeypatch.setattr(status_repo, "_publish_manifest", blocking_publish)
    monkeypatch.setattr(file_records.fcntl, "flock", observed_flock)

    def update_status() -> None:
        try:
            status_repo.update_status(
                key,
                application_status=ApplicationStatus.APPLIED,
                application_status_updated_at="2026-07-13T09:00:00+09:00",
                application_note="지원서 제출",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def save_refresh() -> None:
        try:
            refresh_repo.save(
                replace(
                    stale.record,
                    company="Updated Company",
                    position="Updated Position",
                    source_url="https://example.com/refreshed",
                ),
                jd_markdown="# new",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            refresh_finished.set()

    status_worker = threading.Thread(target=update_status)
    refresh_worker = threading.Thread(target=save_refresh)
    status_worker.start()
    assert publish_entered.wait(timeout=5)
    refresh_worker.start()
    assert refresh_lock_call_entered.wait(timeout=5)
    refresh_waited_for_lock = not refresh_finished.wait(timeout=0.1)

    publish_release.set()
    status_worker.join(timeout=5)
    refresh_worker.join(timeout=5)

    assert refresh_waited_for_lock
    assert errors == []
    assert not status_worker.is_alive()
    assert not refresh_worker.is_alive()
    stored = refresh_repo.get(key)
    assert stored.record.company == "Updated Company"
    assert stored.record.position == "Updated Position"
    assert stored.record.source_url == "https://example.com/refreshed"
    assert [event.note for event in stored.record.application_history] == ["지원서 제출"]
    assert stored.jd_markdown == "# new"


def test_status_append_publish_failure_preserves_bytes_and_rerun_appends_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    before = manifest_path.read_bytes()
    original_publish = repo._publish_manifest

    def fail_publish(record_dir: Path, record: JobRecord, content: Any) -> None:
        raise OSError("injected manifest publication failure")

    monkeypatch.setattr(repo, "_publish_manifest", fail_publish)

    with pytest.raises(OSError, match="injected manifest publication failure"):
        repo.update_status(
            key,
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="2026-07-13T09:00:00+09:00",
            application_note="지원서 제출",
        )

    assert manifest_path.read_bytes() == before
    assert repo.get(key).record.application_history == ()

    monkeypatch.setattr(repo, "_publish_manifest", original_publish)
    updated = repo.update_status(
        key,
        application_status=ApplicationStatus.APPLIED,
        application_status_updated_at="2026-07-13T09:00:00+09:00",
        application_note="지원서 제출",
    )

    assert len(updated.record.application_history) == 1
    assert updated.record.application_history[0].note == "지원서 제출"


def test_v1_read_then_posting_write_persists_synthesized_history_once(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest["record"]["schema_version"] = 1
    manifest["record"]["application_status"] = "applied"
    manifest["record"]["application_status_updated_at"] = "2026-07-13T09:00:00+09:00"
    manifest["record"].pop("application_history")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    legacy_bytes = manifest_path.read_bytes()

    loaded = repo.get(key)

    assert manifest_path.read_bytes() == legacy_bytes
    assert len(loaded.record.application_history) == 1

    repo.update_status(key, posting_status=PostingStatus.CLOSED)
    published = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert published["schema_version"] == 2
    assert published["record"]["schema_version"] == 2
    assert published["record"]["posting_status"] == "closed"
    assert published["record"]["application_history"] == [
        {
            "status": "applied",
            "occurred_at": "2026-07-13T09:00:00+09:00",
            "note": None,
        }
    ]


def test_concurrent_status_updates_preserve_both_completed_events(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "100001")
    repo.create(_record(), jd_markdown="body")
    barrier = threading.Barrier(2)
    results: list[JobRecord] = []
    errors: list[Exception] = []

    def writer(timestamp: str, note: str) -> None:
        try:
            barrier.wait()
            updated = repo.update_status(
                key,
                application_status=ApplicationStatus.INTERVIEW,
                application_status_updated_at=timestamp,
                application_note=note,
            )
            results.append(updated.record)
        except Exception as exc:  # pragma: no cover - failure path asserted below
            errors.append(exc)

    threads = [
        threading.Thread(
            target=writer,
            args=("2026-07-13T09:00:00+09:00", "1차 기술 면접"),
        ),
        threading.Thread(
            target=writer,
            args=("2026-07-14T11:00:00+09:00", "2차 기술 면접"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    fetched = repo.get(key)
    assert {event.note for event in fetched.record.application_history} == {
        "1차 기술 면접",
        "2차 기술 면접",
    }
    assert len(fetched.record.application_history) == 2


def test_update_operations_require_existing_record(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "missing")

    with pytest.raises(JobRecordNotFound):
        repo.update_screening_result(key, screening_markdown="# screening")

    with pytest.raises(JobRecordNotFound):
        repo.update_status(key, application_status=ApplicationStatus.APPLIED)


def test_missing_record_has_distinct_required_and_optional_lookup_semantics(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    key = JobKey("wanted", "missing")

    with pytest.raises(JobRecordNotFound):
        repo.get(key)

    assert repo.find(key) is None


def test_get_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["record"]["platform"] = "remember"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match="identity mismatch"):
        repo.get(JobKey("wanted", "100001"))


def test_provider_and_cap_default_to_absent_on_legacy_records(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["record"].pop("screening_provider", None)
    manifest["record"].pop("verdict_capped", None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))

    fetched = repo.get(JobKey("wanted", "100001"))

    assert fetched.record.screening_provider is None
    assert fetched.record.verdict_capped is False
    assert fetched.record.schema_version == 2


@pytest.mark.parametrize(
    ("outer_version", "inner_version", "message"),
    [
        (2, 1, "manifest schema_version mismatch"),
        (3, 3, "unsupported manifest schema_version"),
    ],
)
def test_get_rejects_manifest_schema_version_drift_before_normalization(
    tmp_path: Path,
    outer_version: int,
    inner_version: int,
    message: str,
) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = outer_version
    manifest["record"]["schema_version"] = inner_version
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match=message):
        repo.get(JobKey("wanted", "100001"))


@pytest.mark.parametrize("payload", [[], "record", None, 1])
def test_get_rejects_non_object_manifest_container(tmp_path: Path, payload: Any) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match="manifest root must be an object"):
        repo.get(JobKey("wanted", "100001"))


@pytest.mark.parametrize("record", [[], "record", None, 1])
def test_get_rejects_non_object_record_container(tmp_path: Path, record: Any) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record"] = record
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match="record must be an object"):
        repo.get(JobKey("wanted", "100001"))


@pytest.mark.parametrize(
    ("target", "value"),
    [
        ("outer", True),
        ("inner", False),
        ("outer", "2"),
        ("inner", 2.0),
        ("outer", None),
        ("inner", None),
        ("outer_missing", None),
        ("inner_missing", None),
    ],
)
def test_get_rejects_non_integer_or_missing_manifest_versions(
    tmp_path: Path,
    target: str,
    value: Any,
) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    manifest_path = tmp_path / "wanted" / "100001" / "record.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if target == "outer_missing":
        manifest.pop("schema_version")
    elif target == "inner_missing":
        manifest["record"].pop("schema_version")
    elif target == "outer":
        manifest["schema_version"] = value
    else:
        manifest["record"]["schema_version"] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(JobRecordIntegrityError, match="schema_version must be an integer"):
        repo.get(JobKey("wanted", "100001"))


def test_screening_result_persists_provider_and_cap(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")

    repo.update_screening_result(
        JobKey("wanted", "100001"),
        screening_markdown="# screening",
        screening_verdict=ScreeningVerdict.HOLD,
        screening_provider="ollama",
        verdict_capped=True,
    )

    fetched = repo.get(JobKey("wanted", "100001"))
    assert fetched.record.screening_provider == "ollama"
    assert fetched.record.verdict_capped is True


def test_screening_result_keeps_cap_when_argument_omitted(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    key = JobKey("wanted", "100001")
    repo.update_screening_result(
        key,
        screening_markdown="# first",
        screening_provider="ollama",
        verdict_capped=True,
    )

    repo.update_screening_result(key, screening_markdown="# second")

    fetched = repo.get(key)
    assert fetched.record.verdict_capped is True
    assert fetched.record.screening_provider == "ollama"


def test_screening_result_clears_cap_when_explicitly_false(tmp_path: Path) -> None:
    repo = JDRecordRepository(tmp_path)
    repo.create(_record(), jd_markdown="body")
    key = JobKey("wanted", "100001")
    repo.update_screening_result(
        key, screening_markdown="# first", screening_provider="ollama", verdict_capped=True
    )

    repo.update_screening_result(
        key, screening_markdown="# second", screening_provider="codex", verdict_capped=False
    )

    fetched = repo.get(key)
    assert fetched.record.verdict_capped is False
    assert fetched.record.screening_provider == "codex"


def test_record_dict_roundtrip_preserves_new_fields() -> None:
    record = _record()
    restored = JobRecord.from_dict(
        {**record.to_dict(), "screening_provider": "local", "verdict_capped": True}
    )

    assert restored.screening_provider == "local"
    assert restored.verdict_capped is True
    assert JobRecord.from_dict(restored.to_dict()) == restored
