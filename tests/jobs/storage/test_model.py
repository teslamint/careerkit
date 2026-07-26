from __future__ import annotations

import pytest

from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict


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
