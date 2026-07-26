from __future__ import annotations

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict


def test_screening_update_preserves_jd_body_and_status_axes(tmp_path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    stored = repository.create(
        JobRecord(
            platform="wanted",
            job_id="123",
            company="TestCo",
            position="Backend",
            application_status=ApplicationStatus.APPLIED,
            posting_status=PostingStatus.CLOSED,
        ),
        jd_markdown="# Backend\n",
    )

    repository.update_screening_result(
        stored.record.key,
        screening_markdown="# Screening\n\n### 최종 판정: 지원 추천\n",
        screening_verdict=ScreeningVerdict.RECOMMENDED,
    )

    updated = repository.get(JobKey("wanted", "123"))
    assert updated.jd_markdown == stored.jd_markdown
    assert updated.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert updated.record.application_status is ApplicationStatus.APPLIED
    assert updated.record.posting_status is PostingStatus.CLOSED
