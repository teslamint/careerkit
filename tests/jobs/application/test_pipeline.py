from __future__ import annotations

import json
from pathlib import Path

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, ScreeningVerdict


def test_queue_status_and_record_operations_use_canonical_storage(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / 'jd' / 'records')
    repository.create(
        JobRecord('wanted', '123', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.HOLD),
        jd_markdown='# JD',
    )
    runtime_dir = tmp_path / 'jd' / 'runtime'
    (runtime_dir / 'queue').mkdir(parents=True)
    (runtime_dir / 'queue' / 'queue.json').write_text(
        json.dumps([{'job_id': '123'}, {'job_id': '124', 'status': 'done'}]),
        encoding='utf-8',
    )
    service = JobsPipelineService(workspace_root=tmp_path, repository=repository, runtime_dir=runtime_dir)

    queue = service.queue_status()
    record = service.show_record(JobKey('wanted', '123'))
    updated = service.set_record_status(
        JobKey('wanted', '123'),
        application_status=ApplicationStatus.APPLIED,
        application_status_updated_at='2026-07-14',
    )

    assert queue.total == 2
    assert queue.counts == {'done': 1, 'pending': 1}
    assert record.record.screening_verdict is ScreeningVerdict.HOLD
    assert updated.record.application_status is ApplicationStatus.APPLIED
    assert updated.record.application_status_updated_at == '2026-07-14'


def test_queue_status_accepts_legacy_object_shape(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / 'jd' / 'records')
    runtime_dir = tmp_path / 'jd' / 'runtime'
    (runtime_dir / 'queue').mkdir(parents=True)
    (runtime_dir / 'queue' / 'queue.json').write_text(
        json.dumps(
            {
                'items': [
                    {'job_id': '1', 'status': 'done'},
                    {'job_id': '2', 'status': 'filtered'},
                    {'job_id': '3'},
                ],
                'stats': {'new': 0},
                'updated_at': '2026-02-01T19:01:06',
            }
        ),
        encoding='utf-8',
    )
    service = JobsPipelineService(workspace_root=tmp_path, repository=repository, runtime_dir=runtime_dir)

    queue = service.queue_status()

    assert queue.total == 3
    assert queue.counts == {'done': 1, 'filtered': 1, 'pending': 1}
