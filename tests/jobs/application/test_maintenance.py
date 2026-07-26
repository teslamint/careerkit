from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from careerkit.jobs.adapters.http import HttpError
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.search import SearchResult
from careerkit.jobs.domain.model import ApplicationStatus, JobRecord, PostingStatus, ScreeningVerdict
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
                'semantic_filter': {'enabled': True, 'model': '~/models/local', 'threshold': -0.25},
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
        def __init__(self, workspace, *, model_name, threshold):
            captured['model_name'] = model_name
            captured['threshold'] = threshold

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
        def __init__(self, workspace, *, model_name, threshold):
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
