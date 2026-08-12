from __future__ import annotations

import pytest

from careerkit.jobs.domain.model import ApplicationEvent, ApplicationStatus, JobKey, JobRecord, PostingStatus, SCHEMA_VERSION, ScreeningVerdict


def test_job_record_round_trip_preserves_axes() -> None:
    record = JobRecord(
        platform="wanted",
        job_id="123",
        company="Acme",
        position="Backend",
        source_url="https://www.wanted.co.kr/wd/123",
        screening_verdict=ScreeningVerdict.HOLD,
        application_status=ApplicationStatus.APPLIED,
        posting_status=PostingStatus.CLOSED,
        application_status_updated_at="2026-07-14",
        application_history=(
            ApplicationEvent(
                status=ApplicationStatus.APPLIED,
                occurred_at="2026-07-14",
                note=None,
            ),
        ),
    )

    restored = JobRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.key == JobKey("wanted", "123")


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_from_dict_rejects_a_non_boolean_verdict_capped(value) -> None:
    """`bool("false")` is True, and a record falsely claiming a cap would be picked
    up and republished by `queue capped --rescreen`."""
    raw = JobRecord("wanted", "123", "Acme", "Backend").to_dict()
    raw["verdict_capped"] = value

    with pytest.raises(ValueError, match="expected boolean"):
        JobRecord.from_dict(raw)


def test_from_dict_defaults_verdict_capped_when_the_field_is_absent() -> None:
    raw = JobRecord("wanted", "123", "Acme", "Backend").to_dict()
    raw.pop("verdict_capped", None)

    assert JobRecord.from_dict(raw).verdict_capped is False


def test_job_key_rejects_traversal_components() -> None:
    with pytest.raises(ValueError, match="Invalid platform"):
        JobKey("../wanted", "1")

    with pytest.raises(ValueError, match="Invalid job_id"):
        JobKey("wanted", "../1")



def test_application_event_trims_blank_note_and_preserves_ordered_history() -> None:
    first = ApplicationEvent(
        status=ApplicationStatus.APPLIED,
        occurred_at="2026-07-14",
        note="  지원서 제출  ",
    )
    second = ApplicationEvent(
        status=ApplicationStatus.INTERVIEW,
        occurred_at="2026-07-15T09:30:00+09:00",
        note="   ",
    )
    record = JobRecord(
        platform="wanted",
        job_id="timeline",
        company="Acme",
        position="Backend",
        application_status=ApplicationStatus.INTERVIEW,
        application_status_updated_at="2026-07-15T09:30:00+09:00",
        application_history=(first, second),
    )

    raw = record.to_dict()

    assert raw["schema_version"] == 2
    assert raw["application_history"] == [
        {
            "status": "applied",
            "occurred_at": "2026-07-14",
            "note": "지원서 제출",
        },
        {
            "status": "interview",
            "occurred_at": "2026-07-15T09:30:00+09:00",
            "note": None,
        },
    ]
    restored = JobRecord.from_dict(raw)
    assert restored.application_history == (first, second)
    assert restored.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize(
    ("occurred_at", "expected"),
    [
        ("2026-07-14", "2026-07-14"),
        ("2026-07-14T03:00:00", "2026-07-14T03:00:00"),
        ("2026-07-14T03:00:00+09:00", "2026-07-14T03:00:00+09:00"),
    ],
)
def test_job_record_from_v1_synthesizes_history_for_parseable_legacy_timestamps(
    occurred_at: str,
    expected: str,
) -> None:
    raw = {
        "platform": "wanted",
        "job_id": "legacy",
        "company": "Acme",
        "position": "Backend",
        "application_status": "applied",
        "application_status_updated_at": occurred_at,
        "schema_version": 1,
    }

    restored = JobRecord.from_dict(raw)

    assert restored.schema_version == 2
    assert restored.application_history == (
        ApplicationEvent(
            status=ApplicationStatus.APPLIED,
            occurred_at=expected,
            note=None,
        ),
    )


def test_job_record_from_v1_keeps_empty_history_without_timestamp() -> None:
    restored = JobRecord.from_dict(
        {
            "platform": "wanted",
            "job_id": "legacy-missing",
            "company": "Acme",
            "position": "Backend",
            "application_status": "applied",
            "schema_version": 1,
        }
    )

    assert restored.schema_version == 2
    assert restored.application_history == ()
    assert restored.application_status_updated_at is None


def test_job_record_from_v1_preserves_unparseable_current_timestamp_without_history() -> None:
    restored = JobRecord.from_dict(
        {
            "platform": "wanted",
            "job_id": "legacy-invalid",
            "company": "Acme",
            "position": "Backend",
            "application_status": "applied",
            "application_status_updated_at": "not-a-timestamp",
            "schema_version": 1,
        }
    )

    assert restored.schema_version == 2
    assert restored.application_history == ()
    assert restored.application_status_updated_at == "not-a-timestamp"


def test_job_record_from_v2_requires_application_history_field() -> None:
    raw = {
        "platform": "wanted",
        "job_id": "v2-missing-history",
        "company": "Acme",
        "position": "Backend",
        "application_status": "applied",
        "application_status_updated_at": "2026-07-14T03:00:00+09:00",
        "schema_version": 2,
    }

    with pytest.raises(ValueError, match="application_history is required for schema_version 2"):
        JobRecord.from_dict(raw)


def test_job_record_from_v2_does_not_synthesize_parseable_current_metadata() -> None:
    raw = {
        "platform": "wanted",
        "job_id": "v2-no-synthesis",
        "company": "Acme",
        "position": "Backend",
        "application_status": "applied",
        "application_status_updated_at": "2026-07-14T03:00:00+09:00",
        "application_history": [],
        "schema_version": 2,
    }

    with pytest.raises(
        ValueError,
        match="schema_version 2 requires application_history to own current application metadata",
    ):
        JobRecord.from_dict(raw)


def test_job_record_from_v2_allows_empty_history_only_for_unparseable_legacy_timestamp() -> None:
    restored = JobRecord.from_dict(
        {
            "platform": "wanted",
            "job_id": "v2-invalid-legacy-current",
            "company": "Acme",
            "position": "Backend",
            "application_status": "applied",
            "application_status_updated_at": "not-a-timestamp",
            "application_history": [],
            "schema_version": 2,
        }
    )

    assert restored.schema_version == 2
    assert restored.application_history == ()
    assert restored.application_status_updated_at == "not-a-timestamp"


@pytest.mark.parametrize(
    ("raw_event", "message"),
    [
        (
            {
                "status": "applied",
                "occurred_at": "not-a-timestamp",
                "note": None,
            },
            "Invalid application event",
        ),
        (
            {
                "status": "applied",
                "occurred_at": "2026-07-14T03:00:00+09:00",
                "note": "x" * 2001,
            },
            "note must be 2000 characters or fewer",
        ),
        (
            {
                "status": "applied",
                "occurred_at": "2026-07-14T03:00:00+09:00",
                "note": None,
                "extra": "field",
            },
            "unknown fields",
        ),
    ],
)
def test_job_record_from_dict_rejects_invalid_application_events(
    raw_event: dict[str, object],
    message: str,
) -> None:
    raw = {
        "platform": "wanted",
        "job_id": "invalid-event",
        "company": "Acme",
        "position": "Backend",
        "application_status": "applied",
        "application_status_updated_at": "2026-07-14T03:00:00+09:00",
        "application_history": [raw_event],
        "schema_version": 2,
    }

    with pytest.raises(ValueError, match=message):
        JobRecord.from_dict(raw)


def test_job_record_from_dict_rejects_mismatched_projection() -> None:
    raw = {
        "platform": "wanted",
        "job_id": "projection-mismatch",
        "company": "Acme",
        "position": "Backend",
        "application_status": "offer",
        "application_status_updated_at": "2026-07-14T03:00:00+09:00",
        "application_history": [
            {
                "status": "applied",
                "occurred_at": "2026-07-14T03:00:00+09:00",
                "note": None,
            }
        ],
        "schema_version": 2,
    }

    with pytest.raises(ValueError, match="application_history must match current projection"):
        JobRecord.from_dict(raw)
