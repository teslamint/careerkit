from __future__ import annotations

import json
from pathlib import Path

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)


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


def test_set_record_status_passes_note_through_repository_boundary(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / 'jd' / 'records')
    repository.create(
        JobRecord('wanted', '123', 'Acme', 'Backend'),
        jd_markdown='# JD',
    )
    service = JobsPipelineService(
        workspace_root=tmp_path,
        repository=repository,
        runtime_dir=tmp_path / 'jd' / 'runtime',
    )

    updated = service.set_record_status(
        JobKey('wanted', '123'),
        application_status=ApplicationStatus.APPLIED,
        application_status_updated_at='2026-07-14T09:00:00+09:00',
        application_note='지원서 제출',
    )

    assert updated.record.application_history[-1].note == '지원서 제출'
    assert updated.record.application_status is ApplicationStatus.APPLIED


def _prescreen_service(tmp_path: Path) -> tuple[JDRecordRepository, JobsPipelineService]:
    repository = JDRecordRepository(tmp_path / 'jd' / 'records')
    service = JobsPipelineService(
        workspace_root=tmp_path,
        repository=repository,
        runtime_dir=tmp_path / 'jd' / 'runtime',
    )
    return repository, service


def _keys(items: list) -> set[JobKey]:
    return {item.record.key for item in items}


def test_list_prescreened_splits_set_aside_from_legacy(tmp_path: Path) -> None:
    repository, service = _prescreen_service(tmp_path)
    repository.create(JobRecord('wanted', '101', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '101'), 'title_exclude')
    repository.create(
        JobRecord('wanted', '102', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.create(
        JobRecord('wanted', '103', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.HOLD),
        jd_markdown='# JD',
    )
    repository.update_screening_result(
        JobKey('wanted', '103'),
        screening_markdown='# Screening',
        screening_verdict=ScreeningVerdict.HOLD,
    )
    repository.create(JobRecord('wanted', '104', 'Acme', 'Backend'), jd_markdown='# JD')

    listing = service.list_prescreened()

    assert _keys(listing.set_aside) == {JobKey('wanted', '101')}
    assert _keys(listing.legacy) == {JobKey('wanted', '102')}
    assert listing.set_aside[0].record.prescreen_reason == 'title_exclude'


def test_list_prescreened_excludes_screened_records_carrying_a_reason(tmp_path: Path) -> None:
    repository, service = _prescreen_service(tmp_path)
    repository.create(JobRecord('wanted', '201', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '201'), 'title_exclude')
    repository.update_screening_result(
        JobKey('wanted', '201'),
        screening_markdown='# Screening',
        screening_verdict=ScreeningVerdict.HOLD,
    )

    listing = service.list_prescreened()

    assert _keys(listing.set_aside) == set()
    assert _keys(listing.legacy) == set()


def test_list_prescreened_filters_by_reason(tmp_path: Path) -> None:
    repository, service = _prescreen_service(tmp_path)
    repository.create(JobRecord('wanted', '301', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '301'), 'title_exclude')
    repository.create(JobRecord('wanted', '302', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '302'), 'backend_override')
    # A legacy record carrying the same reason: without it the filter could be applied to
    # set_aside alone and every assertion here would still hold.
    repository.create(
        JobRecord('remember', '303', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.update_prescreen(JobKey('remember', '303'), 'backend_override')
    repository.create(
        JobRecord('remember', '304', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.update_prescreen(JobKey('remember', '304'), 'title_exclude')

    matched = service.list_prescreened(reason='backend_override')
    unmatched = service.list_prescreened(reason='nonexistent')

    assert _keys(matched.set_aside) == {JobKey('wanted', '302')}
    assert _keys(matched.legacy) == {JobKey('remember', '303')}
    assert unmatched.set_aside == []
    assert unmatched.legacy == []


def test_list_prescreened_excludes_a_closed_legacy_record(tmp_path: Path) -> None:
    repository, service = _prescreen_service(tmp_path)
    repository.create(
        JobRecord('remember', '501', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.create(
        JobRecord('remember', '502', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.update_status(JobKey('remember', '502'), posting_status=PostingStatus.CLOSED)

    listing = service.list_prescreened()

    assert _keys(listing.legacy) == {JobKey('remember', '501')}


def test_queue_prescreened_lists_set_aside_and_legacy_separately(tmp_path: Path) -> None:
    repository, service = _prescreen_service(tmp_path)
    repository.create(JobRecord('wanted', '401', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '401'), 'title_exclude')
    repository.create(
        JobRecord('remember', '402', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.NOT_RECOMMENDED),
        jd_markdown='# JD',
    )
    repository.create(JobRecord('wanted', '403', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_screening_result(
        JobKey('wanted', '403'),
        screening_markdown='# Screening',
        screening_verdict=ScreeningVerdict.RECOMMENDED,
    )
    repository.create(JobRecord('wanted', '404', 'Acme', 'Backend'), jd_markdown='# JD')
    repository.update_prescreen(JobKey('wanted', '404'), 'title_exclude')
    repository.update_status(JobKey('wanted', '404'), posting_status=PostingStatus.CLOSED)

    listing = service.list_prescreened()

    assert _keys(listing.set_aside) == {JobKey('wanted', '401')}
    assert _keys(listing.legacy) == {JobKey('remember', '402')}


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
