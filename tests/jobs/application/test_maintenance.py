from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from careerkit.jobs.adapters.http import HttpError
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.search import SearchResult
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict
from careerkit.workspace import WorkspacePaths


class FakeHttpClient:
    def __init__(self, json_queue=None, text_queue=None) -> None:
        self.json_queue = list(json_queue or [])
        self.text_queue = list(text_queue or [])
        self.requests: list[str] = []

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
        error_cls: type[Exception] = HttpError,
    ) -> dict:
        self.requests.append(url)
        item = self.json_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

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
        self.requests.append(url)
        if not self.text_queue:
            raise AssertionError("request_text should not be used by wanted probes in these tests")
        item = self.text_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_text_no_redirect(self, url: str, **kwargs) -> str:
        return self.request_text(url, **kwargs)


def _wanted_status(status: str) -> dict:
    return {"data": {"job": {"status": status}}}


def test_config_preview_storage_preflight_index_and_summary_rebuild(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                'platforms': {'wanted': {'enabled': True, 'job_group_id': 518, 'job_ids': [872]}},
                'search_queries': ['백엔드'],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding='utf-8',
    )
    service = JobsMaintenanceService(workspace=workspace)
    service.repository.create(
        JobRecord('wanted', '1', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.RECOMMENDED),
        jd_markdown='# JD\n\n| 수집일 | 2026-07-01 |',
    )

    preview = service.config_preview()
    preflight = service.storage_preflight()
    rebuilt = service.rebuild_index()
    summary = service.rebuild_summary()

    assert preview.action == 'apply'
    assert preflight.ready is True
    assert rebuilt.success is True
    assert rebuilt.indexed_count == 1
    assert (tmp_path / 'private' / 'jd' / 'derived' / 'search.sqlite3').exists()
    assert summary.record_count == 1
    assert summary.output_path == tmp_path / 'private' / 'jd' / 'derived' / 'screening-summary.md'
    assert summary.output_path.exists()
    summary_text = summary.output_path.read_text(encoding='utf-8')
    assert '| Platform:ID | 회사 | 포지션 | 판정 | 지원 상태 | 공고 상태 | 수집일 |' in summary_text
    assert '| wanted:1 | Acme | Backend | 지원 추천 | pending | active | 2026-07-01 |' in summary_text
    assert '| 스크리닝 |' not in summary_text


def test_summary_uses_screening_analysis_date_when_collection_date_is_missing(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)
    stored = service.repository.create(
        JobRecord('remember', '2', 'Example', 'Server', screening_verdict=ScreeningVerdict.HOLD),
        jd_markdown='# JD',
    )
    service.repository.update_screening_result(
        stored.record.key,
        screening_markdown='| 분석일 | 2026-06-30 (재스크리닝) |',
        screening_verdict=ScreeningVerdict.HOLD,
    )

    summary = service.rebuild_summary()

    assert '| remember:2 | Example | Server | 지원 보류 | pending | active | 2026-06-30 |' in summary.output_path.read_text(encoding='utf-8')


def test_summary_omits_collection_date_when_creation_time_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)
    service.repository.create(
        JobRecord('wanted', '1', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.HOLD),
        jd_markdown='# JD',
    )
    manifest = tmp_path / 'private' / 'jd' / 'records' / 'wanted' / '1' / 'record.json'
    original_stat = Path.stat

    def stat_without_birthtime(path: Path, *args, **kwargs):
        if path == manifest:
            return SimpleNamespace(st_mtime=1_700_000_000)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat_without_birthtime)

    summary = service.rebuild_summary()

    assert '| wanted:1 | Acme | Backend | 지원 보류 | pending | active | - |' in summary.output_path.read_text(encoding='utf-8')


def test_seen_search_state_is_merged_and_replaced_without_temp_files(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)

    service.persist_seen_job_keys({'wanted:1'})
    service.persist_seen_job_keys({'remember:2'})
    service.persist_seen_job_keys({'wanted:1'}, new_count=0)

    payload = json.loads(service.seen_state_path.read_text(encoding='utf-8'))
    assert payload['seen_job_keys'] == ['remember:2', 'wanted:1']
    assert payload['total_searches'] == 3
    assert payload['total_new_found'] == 2
    assert payload['last_run']
    assert service.search_status().tracked_job_keys == 2
    assert list(service.seen_state_path.parent.glob('.search_state.json.*')) == []

    assert service.reset_search_state() is True
    assert service.search_status().tracked_job_keys == 0
    assert service.reset_search_state() is False

    service.seen_state_path.write_text("{broken", encoding="utf-8")
    assert service.search_status().tracked_job_keys == 0
    service.persist_seen_job_keys({"groupby:3"})
    assert service.search_status().tracked_job_keys == 1


def test_seen_state_does_not_count_canonical_duplicates_as_new(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)

    service.persist_seen_job_keys(
        {'wanted:existing', 'wanted:new'},
        new_count=1,
    )

    status = service.search_status()
    assert status.tracked_job_keys == 2
    assert status.total_new_found == 1


def test_closed_backfill_and_stale_screening_report_preserve_maintenance_tools(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)
    stored = service.repository.create(
        JobRecord('wanted', '1', 'Acme', 'Backend'),
        jd_markdown='# JD\n\n이 포지션은 마감되었습니다.\n',
    )
    service.repository.update_screening_result(
        stored.record.key,
        screening_markdown='# Screening\n',
        screening_verdict=ScreeningVerdict.RECOMMENDED,
    )
    manifest = service.records_root / 'wanted' / '1' / 'record.json'
    stale_time = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(manifest, (stale_time, stale_time))

    preview = service.backfill_closed(dry_run=True)
    stale = service.write_stale_screening_report(days=30)
    applied = service.backfill_closed(dry_run=False)

    assert preview.keys == ('wanted:1',)
    assert preview.changed is False
    assert applied.changed is True
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED
    assert stale.record_count == 1
    assert stale.output_path.read_text(encoding='utf-8').splitlines()[1].startswith(
        'wanted,1,40,'
    )


def test_check_closed_dry_run_lists_closed_without_mutating_and_apply_flips(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close'), _wanted_status('active')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    closing = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.create(JobRecord('wanted', '2', 'Acme', 'Frontend'), jd_markdown='# JD')

    with patch.object(service.repository, 'update_status', wraps=service.repository.update_status) as spy:
        preview = service.check_closed(dry_run=True, delay=0.0)
        assert spy.call_count == 0

    assert preview.closed_keys == ('wanted:1',)
    assert preview.unknown_keys == ()
    assert preview.changed is False
    assert service.repository.get(closing.record.key).record.posting_status is PostingStatus.ACTIVE

    http.json_queue.extend([_wanted_status('close'), _wanted_status('active')])
    applied = service.check_closed(dry_run=False, delay=0.0)

    assert applied.closed_keys == ('wanted:1',)
    assert applied.changed is True
    assert service.repository.get(closing.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_passes_source_url_for_greeting_records(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    url = 'https://acme.career.greetinghr.com/ko/o/100012'
    html = '"openingsInfo":{"openingId":100012,"status":"CLOSED"}'
    http = FakeHttpClient(text_queue=[html])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('greeting', '100012', 'Acme', 'Backend', source_url=url), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0)

    assert http.requests == [url]
    assert result.closed_keys == ('greeting:100012',)


def test_check_closed_skips_closed_records_and_honors_platform_filter(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('active')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    already_closed = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(already_closed.record.key, posting_status=PostingStatus.CLOSED)
    service.repository.create(JobRecord('wanted', '2', 'Acme', 'Frontend'), jd_markdown='# JD')
    service.repository.create(JobRecord('remember', '3', 'Acme', 'DevOps'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0, platforms=('wanted',))

    assert http.requests == ['https://www.wanted.co.kr/api/chaos/jobs/v4/2/details']
    assert result.skipped_platform_counts == {}


def test_check_closed_counts_unsupported_platform_without_http_call(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('headhunter', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0)

    assert result.skipped_platform_counts == {'headhunter': 1}
    assert http.requests == []
    assert result.closed_keys == ()
    assert result.unknown_keys == ()


def test_check_closed_circuit_breaker_trips_after_three_consecutive_unknowns(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[HttpError('boom'), HttpError('boom'), HttpError('boom')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    for job_id in ('1', '2', '3', '4'):
        service.repository.create(JobRecord('wanted', job_id, 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0)

    assert len(http.requests) == 3
    assert result.tripped_platforms == ('wanted',)
    assert set(result.unknown_keys) == {'wanted:1', 'wanted:2', 'wanted:3', 'wanted:4'}
    assert result.closed_keys == ()


def test_check_closed_transport_error_marks_unknown_without_status_change(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[HttpError('boom')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=False, delay=0.0)

    assert result.unknown_keys == ('wanted:1',)
    assert result.closed_keys == ()
    assert result.changed is False
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.ACTIVE


def test_check_closed_second_run_is_idempotent(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    first = service.check_closed(dry_run=False, delay=0.0)
    second = service.check_closed(dry_run=False, delay=0.0)

    assert first.closed_keys == ('wanted:1',)
    assert first.changed is True
    assert second.closed_keys == ()
    assert second.changed is False


def test_check_closed_recheck_dry_run_lists_reopened_without_mutating_and_apply_flips(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('active')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    with patch.object(service.repository, 'update_status', wraps=service.repository.update_status) as spy:
        preview = service.check_closed(dry_run=True, delay=0.0, recheck=True)
        assert spy.call_count == 0

    assert preview.reopened_keys == ('wanted:1',)
    assert preview.closed_keys == ()
    assert preview.changed is False
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED

    http.json_queue.append(_wanted_status('active'))
    applied = service.check_closed(dry_run=False, delay=0.0, recheck=True)

    assert applied.reopened_keys == ('wanted:1',)
    assert applied.changed is True
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.ACTIVE


def test_check_closed_recheck_second_run_is_idempotent(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('active')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    first = service.check_closed(dry_run=False, delay=0.0, recheck=True)
    second = service.check_closed(dry_run=False, delay=0.0, recheck=True)

    assert first.reopened_keys == ('wanted:1',)
    assert first.changed is True
    assert second.reopened_keys == ()
    assert second.changed is False


def test_check_closed_recheck_does_not_probe_active_records(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0, recheck=True)

    assert http.requests == []
    assert result.reopened_keys == ()
    assert result.unknown_keys == ()


def test_check_closed_default_mode_reopened_keys_always_empty(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0)

    assert result.reopened_keys == ()


def test_check_closed_recheck_unknown_probe_leaves_status_closed(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[HttpError('boom')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    result = service.check_closed(dry_run=False, delay=0.0, recheck=True)

    assert result.unknown_keys == ('wanted:1',)
    assert result.reopened_keys == ()
    assert result.changed is False
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_recheck_confirms_still_closed_without_mutation(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    with patch.object(service.repository, 'update_status', wraps=service.repository.update_status) as spy:
        result = service.check_closed(dry_run=False, delay=0.0, recheck=True)
        assert spy.call_count == 0

    assert result.closed_keys == ()
    assert result.reopened_keys == ()
    assert result.unknown_keys == ()
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_recheck_circuit_breaker_trips_and_skips_remaining_closed_records(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(
        json_queue=[
            HttpError('boom'),
            HttpError('boom'),
            HttpError('boom'),
        ]
    )
    service = JobsMaintenanceService(workspace=workspace, http=http)
    for job_id in ('1', '2', '3', '4'):
        stored = service.repository.create(JobRecord('wanted', job_id, 'Acme', 'Backend'), jd_markdown='# JD')
        service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    result = service.check_closed(dry_run=True, delay=0.0, recheck=True)

    assert len(http.requests) == 3
    assert result.tripped_platforms == ('wanted',)
    assert set(result.unknown_keys) == {'wanted:1', 'wanted:2', 'wanted:3', 'wanted:4'}
    assert result.reopened_keys == ()


def test_check_closed_recheck_default_dry_run_is_headless_safe(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('active')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    with patch.object(service.repository, 'update_status', wraps=service.repository.update_status) as spy:
        result = service.check_closed(delay=0.0, recheck=True)
        assert spy.call_count == 0

    assert result.reopened_keys == ('wanted:1',)
    assert result.changed is False
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_recheck_mid_run_abort_leaves_partial_progress_and_rerun_resumes(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('active'), RuntimeError('simulated abort')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    first = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    second = service.repository.create(JobRecord('wanted', '2', 'Acme', 'Frontend'), jd_markdown='# JD')
    service.repository.update_status(first.record.key, posting_status=PostingStatus.CLOSED)
    service.repository.update_status(second.record.key, posting_status=PostingStatus.CLOSED)

    try:
        service.check_closed(dry_run=False, delay=0.0, recheck=True)
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    assert service.repository.get(first.record.key).record.posting_status is PostingStatus.ACTIVE
    assert service.repository.get(second.record.key).record.posting_status is PostingStatus.CLOSED

    http.json_queue.append(_wanted_status('active'))
    resumed = service.check_closed(dry_run=False, delay=0.0, recheck=True)

    assert resumed.reopened_keys == ('wanted:2',)
    assert service.repository.get(second.record.key).record.posting_status is PostingStatus.ACTIVE


def test_check_closed_recheck_groupby_active_demotes_to_unknown_never_reopens(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[{'id': 'still-there'}])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('groupby', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.update_status(stored.record.key, posting_status=PostingStatus.CLOSED)

    result = service.check_closed(dry_run=False, delay=0.0, recheck=True)

    assert result.unknown_keys == ('groupby:1',)
    assert result.reopened_keys == ()
    assert result.changed is False
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_individual_keys_probes_only_specified_records(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    service.repository.create(JobRecord('wanted', '2', 'Acme', 'Frontend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=True, delay=0.0, keys=(JobKey('wanted', '1'),))

    assert result.closed_keys == ('wanted:1',)
    assert len(http.requests) == 1


def test_check_closed_individual_keys_missing_record_goes_to_unknown(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[])
    service = JobsMaintenanceService(workspace=workspace, http=http)

    result = service.check_closed(dry_run=True, delay=0.0, keys=(JobKey('wanted', '999'),))

    assert result.unknown_keys == ('wanted:999',)
    assert result.closed_keys == ()
    assert len(http.requests) == 0


def test_check_closed_individual_keys_apply_updates_status(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[_wanted_status('close')])
    service = JobsMaintenanceService(workspace=workspace, http=http)
    stored = service.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')

    result = service.check_closed(dry_run=False, delay=0.0, keys=(JobKey('wanted', '1'),))

    assert result.closed_keys == ('wanted:1',)
    assert result.changed is True
    assert service.repository.get(stored.record.key).record.posting_status is PostingStatus.CLOSED


def test_check_closed_keys_and_platforms_raises(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = FakeHttpClient(json_queue=[])
    service = JobsMaintenanceService(workspace=workspace, http=http)

    with pytest.raises(ValueError, match='mutually exclusive'):
        service.check_closed(
            dry_run=True, delay=0.0,
            keys=(JobKey('wanted', '1'),), platforms=('wanted',),
        )


def test_search_uses_rejected_records_and_configured_semantic_settings(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private/jd/config/search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                'search': {'role': 'backend'},
                'platforms': {},
                'search_queries': ['Backend'],
                'semantic_filter': {
                    'enabled': True,
                    'model': '~/models/local',
                    'threshold': -0.25,
                    'revision': '1234567890abcdef1234567890abcdef12345678',
                },
            },
            allow_unicode=True,
        ),
        encoding='utf-8',
    )
    service = JobsMaintenanceService(workspace=workspace)
    service.repository.create(
        JobRecord('wanted', '1', 'Rejected Co', 'Backend', application_status=ApplicationStatus.REJECTED),
        jd_markdown='# JD',
    )
    captured = {}

    class FakeSemantic:
        def __init__(self, workspace, *, model_name, threshold, model_revision=None):
            captured['model_name'] = model_name
            captured['threshold'] = threshold
            captured['model_revision'] = model_revision

        def capability(self, *, enabled):
            return type('Capability', (), {'available': True, 'reason': None})()

    class FakeSearchService:
        def __init__(self, **kwargs):
            pass

        def run(self, config, state):
            captured['rejected_companies'] = config.rejected_companies
            return SearchResult((), set())

    monkeypatch.setattr('careerkit.jobs.application.maintenance.SemanticFilterAdapter', FakeSemantic)
    monkeypatch.setattr('careerkit.jobs.application.maintenance.SearchService', FakeSearchService)

    service.search()

    assert captured['model_name'] == str(Path('~/models/local').expanduser())
    assert captured['threshold'] == -0.25
    assert captured['model_revision'] == '1234567890abcdef1234567890abcdef12345678'
    assert captured['rejected_companies'] == {'rejected co'}


def test_search_does_not_inject_semantic_adapter_when_disabled(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private/jd/config/search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                'search': {'role': 'backend'},
                'platforms': {},
                'search_queries': ['Backend'],
                'semantic_filter': {'enabled': False},
            }
        ),
        encoding='utf-8',
    )
    captured = {}

    class FakeSemantic:
        def __init__(self, workspace, *, model_name, threshold, model_revision=None):
            pass

        def capability(self, *, enabled):
            return type('Capability', (), {'available': True, 'reason': None})()

    class FakeSearchService:
        def __init__(self, **kwargs):
            captured['semantic_filter'] = kwargs['semantic_filter']

        def run(self, config, state):
            return SearchResult((), set())

    monkeypatch.setattr('careerkit.jobs.application.maintenance.SemanticFilterAdapter', FakeSemantic)
    monkeypatch.setattr('careerkit.jobs.application.maintenance.SearchService', FakeSearchService)

    JobsMaintenanceService(workspace=workspace).search()

    assert captured['semantic_filter'] is None


class SemanticEvalStubScorer:
    def __init__(self) -> None:
        self.closed = 0

    def prepare(self) -> None:
        return None

    def score_title(self, title: str):
        from careerkit.jobs.application.semantic_eval import SemanticTitleScore

        return SemanticTitleScore(
            title=title,
            normalized_title=' '.join(title.casefold().split()),
            backend_score=0.9,
            non_backend_score=0.1,
            relative_score=0.8,
            reject=False,
        )

    def provenance(self):
        from careerkit.jobs.application.semantic_eval import SemanticModelProvenance

        return SemanticModelProvenance(
            model_name='stub-model',
            model_revision='rev-1',
            sentence_transformers_version='5.1.0',
            anchor_digest='anchor-digest',
            keyword_override_digest='keyword-digest',
            dataset_digest='pending-dataset',
            split_digest='pending-split',
            family_lock_digest='pending-family-lock',
            git_sha='a' * 40,
            command='career-jobs semantic-eval run --dataset <redacted> --output <redacted> --json',
            score_contract_digest='semantic-score-contract/v1',
        )

    def close(self) -> None:
        self.closed += 1


def test_semantic_eval_capture_run_and_compare_publish_private_outputs(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({'search': {'role': 'backend'}, 'platforms': {}, 'search_queries': []}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    service = JobsMaintenanceService(workspace=workspace)

    class StubSearchService:
        def __init__(self, capture_sink) -> None:
            self.capture_sink = capture_sink

        def run(self, config, state):
            del config, state
            assert self.capture_sink is not None
            self.capture_sink.record_source_outcome('wanted', complete=True, stop_reason=None, pages_fetched=1)
            self.capture_sink.capture(
                'SENTINEL PRIVATE BACKEND TITLE',
                quick_filter_outcome='eligible',
                quick_filter_config_digest='quick-filter-v1',
            )
            return SearchResult(postings=(), updated_seen_job_keys=set(), total_found=0, diagnostics=(), capabilities={'semantic_filter': {'available': True}})

    capture_output = tmp_path / 'private' / 'jd' / 'runtime' / 'semantic-eval' / 'capture.json'
    dataset_path = tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'gold.json'
    synthetic_report_root = Path(tempfile.mkdtemp())
    run_output = synthetic_report_root / 'incumbent.json'
    compare_output = synthetic_report_root / 'candidate-vs-incumbent.json'

    for parent in [tmp_path / 'private', tmp_path / 'private' / 'jd', tmp_path / 'private' / 'jd' / 'runtime', tmp_path / 'private' / 'jd' / 'evals', tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter', tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'reports']:
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)

    monkeypatch.setattr(service, '_semantic_search_service', lambda capture_sink=None: (object(), StubSearchService(capture_sink)))
    monkeypatch.setattr(service, '_build_semantic_scorer', lambda: SemanticEvalStubScorer())
    monkeypatch.setattr(service, '_resource_sampler', lambda: {'peak_rss_bytes': 1})
    monkeypatch.setattr(service, '_current_git_sha', lambda: 'a' * 40)
    monkeypatch.setattr(service, '_ensure_private_gold_workspace_clean', lambda dataset: None)

    capture = service.semantic_eval_capture(output_path=capture_output, seed=17)
    queue_payload = json.loads(capture_output.read_text(encoding='utf-8'))
    assert capture.status == 'ok'
    assert queue_payload['schema'] == 'semantic-filter-label-queue/v1'
    assert len(queue_payload['cases']) == 1

    gold_payload = {
        'schema': 'semantic-filter-eval/v1',
        'dataset_version': '2026-08-10',
        'source_queue_digests': [queue_payload['source_queue_digest']],
        'human_confirmation': {'confirmed_by': 'tester', 'confirmed_at': '2026-08-10T02:00:00Z'},
        'lock_timestamp': '2026-08-10T02:30:00Z',
        'lock_digest': '',
        'evidence_tier': 'synthetic',
        'cases': [
            {
                'case_id': 'case-private-1',
                'family_id': 'family-private-1',
                'title': 'SENTINEL PRIVATE BACKEND TITLE',
                'label': 'backend',
                'split': 'holdout',
                'slices': ['english', 'generic_software'],
                'quick_filter_outcome': 'eligible',
                'quick_filter_config_digest': 'quick-filter-v1',
            }
        ],
    }
    from careerkit.jobs.application.semantic_eval import compute_lock_digest, load_dataset_payload
    queue = load_dataset_payload({**gold_payload, 'lock_digest': 'temporary'})
    gold_payload['lock_digest'] = compute_lock_digest(tuple(gold_payload['source_queue_digests']), queue.cases)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(json.dumps(gold_payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    dataset_path.chmod(0o600)

    run = service.semantic_eval_run(dataset_path=dataset_path, output_path=run_output)
    run_payload = json.loads(run_output.read_text(encoding='utf-8'))
    assert run.status == 'insufficient_data'
    assert run_payload['schema'] == 'semantic-filter-score-report/v1'
    assert run_payload['case_scores'][0]['case_id'] == 'case-private-1'
    run_command = run_payload['provenance']['command']
    assert str(run_output.resolve()) in run_command
    assert 'private/jd/evals' not in run_command
    assert '<redacted>' in run_command

    candidate_output = synthetic_report_root / 'candidate.json'
    candidate_output.write_text(run_output.read_text(encoding='utf-8'), encoding='utf-8')
    candidate_output.chmod(0o600)
    compare = service.semantic_eval_compare(dataset_path=dataset_path, incumbent_path=run_output, candidate_path=candidate_output, output_path=compare_output)
    compare_payload = json.loads(compare_output.read_text(encoding='utf-8'))
    assert compare.status == 'fail'
    assert compare_payload['schema'] == 'semantic-filter-comparison-report/v1'
    assert compare_payload['comparison']['reason'] == 'candidate_not_authorized'


def test_semantic_eval_rejects_wrong_artifact_class_roots_and_validates_round_trip(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({'search': {'role': 'backend'}, 'platforms': {}, 'search_queries': []}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    service = JobsMaintenanceService(workspace=workspace)

    for parent in [
        tmp_path / 'private',
        tmp_path / 'private' / 'jd',
        tmp_path / 'private' / 'jd' / 'runtime',
        tmp_path / 'private' / 'jd' / 'runtime' / 'semantic-eval',
        tmp_path / 'private' / 'jd' / 'evals',
        tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter',
        tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'reports',
    ]:
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)

    with pytest.raises(ValueError, match='capture output path'):
        service.semantic_eval_capture(output_path=tmp_path / 'private' / 'jd' / 'derived' / 'bad.json', seed=17)

    synthetic_dataset_path = tmp_path / 'synthetic-gold.json'
    synthetic_output_path = Path(tempfile.mkdtemp()) / 'synthetic-report.json'
    wrong_private_output = tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'reports' / 'synthetic-report.json'

    gold_payload = {
        'schema': 'semantic-filter-eval/v1',
        'dataset_version': '2026-08-10',
        'source_queue_digests': ['queue-a'],
        'human_confirmation': {'confirmed_by': 'tester', 'confirmed_at': '2026-08-10T02:00:00Z'},
        'lock_timestamp': '2026-08-10T02:30:00Z',
        'lock_digest': '',
        'evidence_tier': 'synthetic',
        'cases': [
            {
                'case_id': 'case-private-1',
                'family_id': 'family-private-1',
                'title': 'SENTINEL PRIVATE BACKEND TITLE',
                'label': 'backend',
                'split': 'holdout',
                'slices': ['english', 'generic_software'],
                'quick_filter_outcome': 'eligible',
                'quick_filter_config_digest': 'quick-filter-v1',
            }
        ],
    }
    from careerkit.jobs.application.semantic_eval import compute_lock_digest, load_dataset_payload
    queue = load_dataset_payload({**gold_payload, 'lock_digest': 'temporary'})
    gold_payload['lock_digest'] = compute_lock_digest(tuple(gold_payload['source_queue_digests']), queue.cases)
    synthetic_dataset_path.write_text(json.dumps(gold_payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    synthetic_dataset_path.chmod(0o600)

    monkeypatch.setattr(service, '_build_semantic_scorer', lambda: SemanticEvalStubScorer())
    monkeypatch.setattr(service, '_resource_sampler', lambda: {'peak_rss_bytes': 1})
    monkeypatch.setattr(service, '_current_git_sha', lambda: 'a' * 40)

    with pytest.raises(ValueError, match='synthetic temp root'):
        service.semantic_eval_run(dataset_path=synthetic_dataset_path, output_path=wrong_private_output)

    original_payload_builder = service._semantic_eval_report_payload

    def tampered_payload(report):
        payload = original_payload_builder(report)
        payload['counts'] = {'holdout_cases': 999}
        return payload

    monkeypatch.setattr(service, '_semantic_eval_report_payload', tampered_payload)
    with pytest.raises(ValueError, match='count evidence mismatch'):
        service.semantic_eval_run(dataset_path=synthetic_dataset_path, output_path=synthetic_output_path)
    assert synthetic_output_path.exists() is False


def test_semantic_eval_compare_rejects_wrong_output_root(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({'search': {'role': 'backend'}, 'platforms': {}, 'search_queries': []}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    service = JobsMaintenanceService(workspace=workspace)

    for parent in [
        tmp_path / 'private',
        tmp_path / 'private' / 'jd',
        tmp_path / 'private' / 'jd' / 'runtime',
        tmp_path / 'private' / 'jd' / 'evals',
        tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter',
        tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'reports',
    ]:
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)

    dataset_path = tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'gold.json'
    synthetic_report_root = Path(tempfile.mkdtemp())
    run_output = synthetic_report_root / 'incumbent.json'
    candidate_output = synthetic_report_root / 'candidate.json'
    wrong_output = tmp_path / 'private' / 'jd' / 'runtime' / 'semantic-eval' / 'compare.json'

    gold_payload = {
        'schema': 'semantic-filter-eval/v1',
        'dataset_version': '2026-08-10',
        'source_queue_digests': ['queue-a'],
        'human_confirmation': {'confirmed_by': 'tester', 'confirmed_at': '2026-08-10T02:00:00Z'},
        'lock_timestamp': '2026-08-10T02:30:00Z',
        'lock_digest': '',
        'evidence_tier': 'synthetic',
        'cases': [
            {
                'case_id': 'case-private-1',
                'family_id': 'family-private-1',
                'title': 'SENTINEL PRIVATE BACKEND TITLE',
                'label': 'backend',
                'split': 'holdout',
                'slices': ['english', 'generic_software'],
                'quick_filter_outcome': 'eligible',
                'quick_filter_config_digest': 'quick-filter-v1',
            }
        ],
    }
    from careerkit.jobs.application.semantic_eval import compute_lock_digest, load_dataset_payload
    queue = load_dataset_payload({**gold_payload, 'lock_digest': 'temporary'})
    gold_payload['lock_digest'] = compute_lock_digest(tuple(gold_payload['source_queue_digests']), queue.cases)
    dataset_path.write_text(json.dumps(gold_payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    dataset_path.chmod(0o600)

    monkeypatch.setattr(service, '_build_semantic_scorer', lambda: SemanticEvalStubScorer())
    monkeypatch.setattr(service, '_resource_sampler', lambda: {'peak_rss_bytes': 1})
    monkeypatch.setattr(service, '_current_git_sha', lambda: 'a' * 40)

    service.semantic_eval_run(dataset_path=dataset_path, output_path=run_output)
    candidate_output.write_text(run_output.read_text(encoding='utf-8'), encoding='utf-8')
    candidate_output.chmod(0o600)

    with pytest.raises(ValueError, match='synthetic temp root'):
        service.semantic_eval_compare(dataset_path=dataset_path, incumbent_path=run_output, candidate_path=candidate_output, output_path=wrong_output)


def test_private_gold_locked_dataset_rejects_temp_root_after_load(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config_path = tmp_path / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({'search': {'role': 'backend'}, 'platforms': {}, 'search_queries': []}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    service = JobsMaintenanceService(workspace=workspace)

    dataset_path = Path(tempfile.mkdtemp()) / 'private-gold.json'
    holdout_cases = []
    index = 0
    from careerkit.jobs.application.semantic_eval import authoritative_split
    while len(holdout_cases) < 299:
        family_id = f'fam-{index}'
        if authoritative_split('2026-08-10', family_id) == 'holdout':
            holdout_cases.append({
                'case_id': f'case-{index}',
                'family_id': family_id,
                'title': f'Backend {index}',
                'label': 'backend',
                'split': 'holdout',
                'slices': ['english', 'generic_software'],
                'quick_filter_outcome': 'eligible',
                'quick_filter_config_digest': 'quick-filter-v1',
            })
        index += 1
    gold_payload = {
        'schema': 'semantic-filter-eval/v1',
        'dataset_version': '2026-08-10',
        'source_queue_digests': ['queue-a'],
        'human_confirmation': {'confirmed_by': 'tester', 'confirmed_at': '2026-08-10T02:00:00Z'},
        'lock_timestamp': '2026-08-10T02:30:00Z',
        'lock_digest': '',
        'evidence_tier': 'synthetic',
        'cases': holdout_cases,
    }
    from careerkit.jobs.application.semantic_eval import compute_lock_digest, load_dataset_payload
    queue = load_dataset_payload({**gold_payload, 'lock_digest': 'temporary'})
    gold_payload['lock_digest'] = compute_lock_digest(tuple(gold_payload['source_queue_digests']), queue.cases)
    gold_payload['evidence_tier'] = 'private_gold_locked'
    dataset_path.write_text(json.dumps(gold_payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    dataset_path.chmod(0o600)

    monkeypatch.setattr(service, '_build_semantic_scorer', lambda: SemanticEvalStubScorer())
    monkeypatch.setattr(service, '_resource_sampler', lambda: {'peak_rss_bytes': 1})
    monkeypatch.setattr(service, '_current_git_sha', lambda: 'a' * 40)

    with pytest.raises(ValueError, match='private eval root'):
        service.semantic_eval_run(dataset_path=dataset_path, output_path=tmp_path / 'private' / 'jd' / 'evals' / 'semantic-filter' / 'reports' / 'report.json')


def test_runner_temp_is_an_approved_synthetic_root(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)
    runner_temp = tmp_path / 'runner-temp'
    runner_temp.mkdir(mode=0o700)
    monkeypatch.setenv('RUNNER_TEMP', str(runner_temp))

    roots = service._semantic_temp_roots()

    assert runner_temp.resolve() in roots


def test_semantic_eval_run_uses_source_checkout_git_sha_not_workspace_sha(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / 'data-workspace'
    workspace = WorkspacePaths(root=workspace_root, source='explicit')
    config_path = workspace_root / 'private' / 'jd' / 'config' / 'search_config.yaml'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({'search': {'role': 'backend'}, 'platforms': {}, 'search_queries': []}, allow_unicode=True, sort_keys=False), encoding='utf-8')
    service = JobsMaintenanceService(workspace=workspace)

    private_eval_root = workspace_root / 'private' / 'jd' / 'evals' / 'semantic-filter'
    private_eval_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (private_eval_root / 'reports').mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in [workspace_root / 'private', workspace_root / 'private' / 'jd', private_eval_root, private_eval_root / 'reports']:
        parent.chmod(0o700)

    source_repo = tmp_path / 'source-checkout'
    source_repo.mkdir()
    (source_repo / '.gitkeep').write_text('source\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(source_repo)], check=True)
    subprocess.run(['git', '-C', str(source_repo), 'add', '.gitkeep'], check=True)
    subprocess.run(['git', '-C', str(source_repo), '-c', 'commit.gpgsign=false', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-q', '-m', 'source'], check=True)
    source_sha = subprocess.run(['git', '-C', str(source_repo), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()

    (workspace_root / '.gitkeep').write_text('data\n', encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(workspace_root)], check=True)
    subprocess.run(['git', '-C', str(workspace_root), 'add', '.gitkeep'], check=True)
    subprocess.run(['git', '-C', str(workspace_root), '-c', 'commit.gpgsign=false', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-q', '-m', 'data'], check=True)
    workspace_sha = subprocess.run(['git', '-C', str(workspace_root), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    assert source_sha != workspace_sha

    dataset_path = private_eval_root / 'gold.json'
    output_path = Path(tempfile.mkdtemp()) / 'incumbent.json'
    gold_payload = {
        'schema': 'semantic-filter-eval/v1',
        'dataset_version': '2026-08-10',
        'source_queue_digests': ['queue-a'],
        'human_confirmation': {'confirmed_by': 'tester', 'confirmed_at': '2026-08-10T02:00:00Z'},
        'lock_timestamp': '2026-08-10T02:30:00Z',
        'lock_digest': '',
        'evidence_tier': 'synthetic',
        'cases': [
            {
                'case_id': 'case-private-1',
                'family_id': 'family-private-1',
                'title': 'SENTINEL PRIVATE BACKEND TITLE',
                'label': 'backend',
                'split': 'holdout',
                'slices': ['english', 'generic_software'],
                'quick_filter_outcome': 'eligible',
                'quick_filter_config_digest': 'quick-filter-v1',
            }
        ],
    }
    from careerkit.jobs.application.semantic_eval import compute_lock_digest, load_dataset_payload
    queue = load_dataset_payload({**gold_payload, 'lock_digest': 'temporary'})
    gold_payload['lock_digest'] = compute_lock_digest(tuple(gold_payload['source_queue_digests']), queue.cases)
    dataset_path.write_text(json.dumps(gold_payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    dataset_path.chmod(0o600)

    monkeypatch.setattr(service, '_build_semantic_scorer', lambda: SemanticEvalStubScorer())
    monkeypatch.setattr(service, '_resource_sampler', lambda: {'peak_rss_bytes': 1})
    monkeypatch.setattr('careerkit.jobs.application.maintenance.inspect.getsourcefile', lambda obj: str(source_repo / 'src' / 'careerkit' / 'jobs' / 'application' / 'maintenance.py'))

    result = service.semantic_eval_run(dataset_path=dataset_path, output_path=output_path)
    payload = json.loads(output_path.read_text(encoding='utf-8'))

    assert result.status == 'insufficient_data'
    assert payload['provenance']['git_sha'] == source_sha
    assert payload['provenance']['git_sha'] != workspace_sha


def test_current_git_sha_fails_closed_when_source_root_exists_but_git_lookup_fails(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / 'data-workspace'
    workspace_root.mkdir()
    workspace = WorkspacePaths(root=workspace_root, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)

    source_root = tmp_path / 'source-checkout'
    source_root.mkdir()

    workspace_sha = 'b' * 40

    monkeypatch.setattr(service, '_source_checkout_root', lambda: source_root)

    def fake_run(args, check=False, capture_output=True, text=True):
        target = Path(args[2])
        if target == source_root:
            return SimpleNamespace(returncode=128, stdout='', stderr='fatal: not a git repository')
        if target == workspace_root:
            return SimpleNamespace(returncode=0, stdout=f'{workspace_sha}\n', stderr='')
        raise AssertionError(args)

    monkeypatch.setattr('careerkit.jobs.application.maintenance.subprocess.run', fake_run)

    with pytest.raises(ValueError, match='missing git sha'):
        service._current_git_sha()


def test_current_git_sha_falls_back_to_workspace_when_no_source_root_exists(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / 'data-workspace'
    workspace_root.mkdir()
    workspace = WorkspacePaths(root=workspace_root, source='explicit')
    service = JobsMaintenanceService(workspace=workspace)

    workspace_sha = 'c' * 40

    monkeypatch.setattr(service, '_source_checkout_root', lambda: None)

    def fake_run(args, check=False, capture_output=True, text=True):
        target = Path(args[2])
        if target == workspace_root:
            return SimpleNamespace(returncode=0, stdout=f'{workspace_sha}\n', stderr='')
        raise AssertionError(args)

    monkeypatch.setattr('careerkit.jobs.application.maintenance.subprocess.run', fake_run)

    assert service._current_git_sha() == workspace_sha
