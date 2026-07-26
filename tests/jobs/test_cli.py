from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from careerkit.jobs import cli
from careerkit.jobs.application.automation import (
    AutomationRunResult,
    AutomationService,
    JobsCompletionStage,
    JobsExtractionStage,
    JobsResumeStateService,
    JobsScreeningStage,
    load_candidate_context,
)
from careerkit.jobs.application.company_info import CompanyValidationSummary
from careerkit.jobs.application.maintenance import CheckClosedResult
from careerkit.jobs.application.pipeline import IngestResult, QueueStatusResult
from careerkit.jobs.application.preflight import PreflightFinding, StoragePreflightResult
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict
from careerkit.workspace import WorkspacePaths


@dataclass
class FakeMaintenance:
    persisted_seen: list[set[str]] | None = None
    cleaned_preflights: list[Path] | None = None
    search_calls: list[tuple[tuple[str, ...] | None, int | None]] | None = None
    check_closed_calls: list[dict[str, object]] | None = None

    def relative_path(self, path: Path) -> str:
        return str(path)

    def config_check(self):
        from careerkit.jobs.application.config import ConfigCheckResult, ConfigDiagnostic
        return ConfigCheckResult(ready=False, action='apply', normalized_role='backend', findings=(ConfigDiagnostic('legacy_native_role_mapping', 'convert'),))

    def config_preview(self):
        from careerkit.jobs.application.config import ConfigPreviewResult, ConfigDiagnostic
        return ConfigPreviewResult(ready=False, action='apply', normalized_role='backend', diagnostics=(ConfigDiagnostic('legacy_native_role_mapping', 'convert'),), converted_config={}, would_write=False)

    def config_apply(self):
        from careerkit.jobs.application.config import ConfigApplyResult, ConfigDiagnostic
        return ConfigApplyResult(action='apply', changed=True, diagnostics=(ConfigDiagnostic('legacy_native_role_mapping', 'convert'),), config={}, backup_path=Path('backup'))

    def search(self, *, queries=None, max_urls=None):
        from careerkit.jobs.application.search import SearchCandidate, SearchResult
        if self.search_calls is None:
            self.search_calls = []
        self.search_calls.append((tuple(queries) if queries else None, max_urls))
        return SearchResult(postings=(SearchCandidate('wanted', '1', 'wanted:1', 'Backend', 'Acme', '3년', 'https://www.wanted.co.kr/wd/1'),), updated_seen_job_keys={'wanted:1'}, total_found=1)

    def persist_seen_job_keys(
        self, seen_job_keys: set[str], *, new_count: int | None = None
    ) -> None:
        if self.persisted_seen is None:
            self.persisted_seen = []
        self.persisted_seen.append(set(seen_job_keys))

    def search_status(self):
        from careerkit.jobs.application.maintenance import SearchStatusResult
        return SearchStatusResult('2026-07-15T00:00:00', 3, 7, 5)

    def reset_search_state(self) -> bool:
        return True

    def backfill_closed(self, *, dry_run=True):
        from careerkit.jobs.application.maintenance import ClosedBackfillResult
        return ClosedBackfillResult(('wanted:1',), changed=not dry_run)

    def check_closed(self, *, dry_run=True, delay=1.0, platforms=None, recheck=False):
        from careerkit.jobs.application.maintenance import CheckClosedResult
        if self.check_closed_calls is None:
            self.check_closed_calls = []
        self.check_closed_calls.append(
            {'dry_run': dry_run, 'delay': delay, 'platforms': platforms, 'recheck': recheck}
        )
        return CheckClosedResult(
            closed_keys=('wanted:1',),
            unknown_keys=(),
            skipped_platform_counts={},
            tripped_platforms=(),
            changed=not dry_run,
            reopened_keys=('wanted:2',) if recheck else (),
        )

    def write_stale_screening_report(self, *, days=30, output_path=None):
        from careerkit.jobs.application.maintenance import StaleScreeningReport
        return StaleScreeningReport(
            output_path=output_path or Path('private/jd/derived/stale-screening.csv'),
            record_count=2,
        )

    def storage_preflight(self):
        return StoragePreflightResult(ready=True, record_count=1, screening_count=1, checked_keys=('wanted:1',), schema_version=1, isolated_output_root=Path('tmp/preflight'), findings=(PreflightFinding('ok', 'clear', 'wanted:1'),), status_counts={'records:total': 1})

    def cleanup_preflight(self, output_root: Path) -> None:
        if self.cleaned_preflights is None:
            self.cleaned_preflights = []
        self.cleaned_preflights.append(output_root)

    def rebuild_index(self, *, database_path=None):
        from careerkit.jobs.adapters.storage.sqlite_index import IndexRebuildReport
        return IndexRebuildReport(success=True, indexed_count=1)

    derived_dir = Path('private/jd/derived')

    def rebuild_summary(self, *, output_path=None):
        from careerkit.jobs.application.maintenance import SummaryRebuildResult
        return SummaryRebuildResult(output_path=Path('private/jd/derived/screening-summary.md'), record_count=1)

    def company_validate(self, *, file_name=None, fix=False):
        return CompanyValidationSummary(
            processed_files=1,
            error_files=0,
            critical_risk_companies=0,
            high_risk_companies=1,
            incomplete_companies=1,
            results=(),
            errors=(),
            fixed_files=('acme.md',) if fix else (),
            report_path=None,
        )


@dataclass
class FakePipeline:
    status_calls: list[dict[str, object]] | None = None
    classify_error: Exception | None = None

    def ingest_url(self, url: str) -> IngestResult:
        return IngestResult(source=url, job_id='1', outcome='needs_manual', message='extract me')

    def ingest_file(self, path: Path):
        return [IngestResult(source=str(path), job_id='1', outcome='needs_manual', message='extract me')]

    def show_record(self, key: JobKey):
        from careerkit.jobs.adapters.storage.file_records import StoredJobMetadata
        return StoredJobMetadata(record=JobRecord('wanted', '1', 'Acme', 'Backend', screening_verdict=ScreeningVerdict.HOLD), has_screening=True)

    def set_record_status(self, key: JobKey, **kwargs):
        from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
        if self.status_calls is None:
            self.status_calls = []
        self.status_calls.append(kwargs)
        return StoredJobRecord(record=JobRecord('wanted', '1', 'Acme', 'Backend', application_status=ApplicationStatus.APPLIED, posting_status=PostingStatus.ACTIVE, application_status_updated_at='2026-07-14'), jd_markdown='# JD', screening_markdown=None)

    def set_record_verdict(self, key: JobKey, verdict: ScreeningVerdict):
        from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
        return StoredJobRecord(record=JobRecord('wanted', '1', 'Acme', 'Backend', screening_verdict=verdict), jd_markdown='# JD', screening_markdown=None)

    def queue_status(self):
        return QueueStatusResult(total=2, counts={'pending': 2})

    def migrate_queue_status(self):
        return [{'job_key': 'wanted/1', 'status': 'pending', 'result': 'skipped'}]

    def classify_record(self, key: JobKey, *, dry_run: bool = False):
        if self.classify_error is not None:
            raise self.classify_error
        return IngestResult(source='wanted/1', job_id='1', outcome='success', message='classified')

    def rescreen_record(self, key: JobKey, *, dry_run: bool = False):
        return [IngestResult(source='wanted/1', job_id='1', outcome='success', message='rescanned')]

    def storage_status(self):
        return {'records:total': 1}


@dataclass
class FakeAutomation:
    calls: list[tuple[str, list[str]]] | None = None

    def run(self, operation: str, args: list[str]) -> AutomationRunResult:
        if self.calls is None:
            self.calls = []
        self.calls.append((operation, list(args)))
        return AutomationRunResult(returncode=0, stdout='{"status":"ok"}\n', stderr='')


def test_cli_json_commands_dispatch_through_service_bundle(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(['config', 'check', '--json'])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload['command'] == 'config check'
    assert payload['action'] == 'apply'

    exit_code = cli.main(['storage', 'preflight', '--json'])
    storage_output = capsys.readouterr().out
    payload = json.loads(storage_output)
    assert exit_code == 0
    assert payload['record_count'] == 1
    assert payload['finding_codes'] == ['ok']
    assert 'wanted:1' not in storage_output
    assert 'checked_keys' not in payload
    assert 'isolated_output_root' not in payload
    assert maintenance.cleaned_preflights == [Path('tmp/preflight')]

    exit_code = cli.main(['summary', 'rebuild', '--json'])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload['output_path'] == 'private/jd/derived/screening-summary.md'


def test_cli_run_auto_and_record_show_dispatch(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    automation = FakeAutomation()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=automation,
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['run', 'auto', '--search-only', '--json']) == 0
    assert automation.calls == [('auto', ['--search-only', '--json'])]
    assert capsys.readouterr().out == '{"status":"ok"}\n'

    assert cli.main(['record', 'show', 'wanted:1', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['job_key'] == 'wanted:1'
    assert payload['has_screening'] is True


def test_ingest_json_payload_uses_compound_job_key() -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    payload = cli._payload_from_ingest(
        'ingest url',
        workspace,
        IngestResult(
            source='https://www.wanted.co.kr/wd/123',
            job_id='123',
            outcome='needs_manual',
            message='extract',
        ),
    )
    assert payload['job_key'] == 'wanted:123'


def test_check_closed_payload_projects_populated_close_result() -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')

    payload = cli._check_closed_payload(
        workspace=workspace,
        result=CheckClosedResult(
            closed_keys=('wanted:1', 'jumpit:2'),
            reopened_keys=('wanted:3',),
            unknown_keys=('saramin:한글',),
            skipped_platform_counts={'remember': 2},
            tripped_platforms=('jumpit',),
            changed=True,
        ),
        apply=True,
        recheck=False,
    )

    assert payload == {
        'command': 'record check-closed',
        'workspace_root': '.',
        'workspace_source': 'explicit',
        'mode': 'close',
        'apply': True,
        'dry_run': False,
        'changed': True,
        'closed_keys': ['wanted:1', 'jumpit:2'],
        'reopened_keys': ['wanted:3'],
        'unknown_keys': ['saramin:한글'],
        'skipped_platform_counts': {'remember': 2},
        'tripped_platforms': ['jumpit'],
    }


def test_check_closed_payload_projects_empty_reopen_result_as_json_native_values(
    capsys,
) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    result = CheckClosedResult(
        closed_keys=(),
        reopened_keys=(),
        unknown_keys=(),
        skipped_platform_counts={},
        tripped_platforms=(),
        changed=False,
    )

    payload = cli._check_closed_payload(
        workspace=workspace,
        result=result,
        apply=False,
        recheck=True,
    )

    assert payload['mode'] == 'reopen'
    assert payload['apply'] is False
    assert payload['dry_run'] is True
    assert payload['closed_keys'] == []
    assert payload['reopened_keys'] == []
    assert payload['unknown_keys'] == []
    assert payload['skipped_platform_counts'] == {}
    assert payload['tripped_platforms'] == []

    cli._print_json(payload)
    assert json.loads(capsys.readouterr().out) == payload


def test_print_check_closed_human_lists_populated_sections_in_stable_format(capsys) -> None:
    cli._print_check_closed_human(
        {
            'mode': 'close',
            'apply': True,
            'dry_run': False,
            'changed': True,
            'closed_keys': ['wanted:1', 'jumpit:2'],
            'reopened_keys': ['wanted:3'],
            'unknown_keys': ['saramin:4'],
            'skipped_platform_counts': {'remember': 2, 'groupby': 1},
            'tripped_platforms': ['jumpit'],
        }
    )

    assert capsys.readouterr().out == (
        'mode=close apply=true dry_run=false changed=true\n'
        'closed (2):\n'
        '- wanted:1\n'
        '- jumpit:2\n'
        'reopened (1):\n'
        '- wanted:3\n'
        'unknown (1):\n'
        '- saramin:4\n'
        'skipped (2):\n'
        '- groupby: 1\n'
        '- remember: 2\n'
        'tripped (1):\n'
        '- jumpit\n'
    )


def test_print_check_closed_human_retains_all_empty_sections(capsys) -> None:
    cli._print_check_closed_human(
        {
            'mode': 'reopen',
            'apply': False,
            'dry_run': True,
            'changed': False,
            'closed_keys': [],
            'reopened_keys': [],
            'unknown_keys': [],
            'skipped_platform_counts': {},
            'tripped_platforms': [],
        }
    )

    assert capsys.readouterr().out == (
        'mode=reopen apply=false dry_run=true changed=false\n'
        'closed (0):\n- none\n'
        'reopened (0):\n- none\n'
        'unknown (0):\n- none\n'
        'skipped (0):\n- none\n'
        'tripped (0):\n- none\n'
    )


def test_cli_record_status_defaults_application_timestamp(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    pipeline = FakePipeline()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    frozen = SimpleNamespace(now=lambda: SimpleNamespace(isoformat=lambda: '2026-07-15T03:00:00'))
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'datetime', frozen)

    assert cli.main(
        ['record', 'set-status', 'wanted:1', '--application-status', 'applied', '--json']
    ) == 0
    capsys.readouterr()
    assert pipeline.status_calls == [
        {
            'application_status': ApplicationStatus.APPLIED,
            'posting_status': None,
            'application_status_updated_at': '2026-07-15T03:00:00',
        }
    ]


def test_cli_record_check_closed_defaults_to_dry_run_all_platforms(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'check-closed']) == 0
    assert maintenance.check_closed_calls == [
        {'dry_run': True, 'delay': 1.0, 'platforms': None, 'recheck': False}
    ]
    out = capsys.readouterr().out
    assert out == (
        'mode=close apply=false dry_run=true changed=false\n'
        'closed (1):\n- wanted:1\n'
        'reopened (0):\n- none\n'
        'unknown (0):\n- none\n'
        'skipped (0):\n- none\n'
        'tripped (0):\n- none\n'
    )


def test_cli_record_check_closed_apply_and_repeated_platform(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'check-closed', '--apply', '--platform', 'wanted']) == 0
    assert maintenance.check_closed_calls == [
        {'dry_run': False, 'delay': 1.0, 'platforms': ('wanted',), 'recheck': False}
    ]

    assert cli.main(
        ['record', 'check-closed', '--platform', 'wanted', '--platform', 'remember']
    ) == 0
    assert maintenance.check_closed_calls is not None
    assert maintenance.check_closed_calls[-1] == {
        'dry_run': True,
        'delay': 1.0,
        'platforms': ('wanted', 'remember'),
        'recheck': False,
    }


def test_cli_record_check_closed_json_is_exclusive_and_forwards_platforms(
    monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        [
            'record',
            'check-closed',
            '--json',
            '--platform',
            'wanted',
            '--platform',
            'remember',
        ]
    ) == 0

    assert maintenance.check_closed_calls == [
        {
            'dry_run': True,
            'delay': 1.0,
            'platforms': ('wanted', 'remember'),
            'recheck': False,
        }
    ]
    captured = capsys.readouterr()
    assert captured.err == ''
    payload = json.loads(captured.out)
    assert captured.out == json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n'
    assert payload == {
        'command': 'record check-closed',
        'workspace_root': '.',
        'workspace_source': 'explicit',
        'mode': 'close',
        'apply': False,
        'dry_run': True,
        'changed': False,
        'closed_keys': ['wanted:1'],
        'reopened_keys': [],
        'unknown_keys': [],
        'skipped_platform_counts': {},
        'tripped_platforms': [],
    }


def test_cli_record_check_closed_rejects_unsupported_platform(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(['record', 'check-closed', '--platform', 'nope', '--json'])

    assert exit_code != 0
    assert maintenance.check_closed_calls is None
    captured = capsys.readouterr()
    assert captured.out == ''
    stderr = captured.err
    assert 'nope' in stderr
    for platform in cli.SUPPORTED_PLATFORMS:
        assert platform in stderr


def test_cli_record_check_closed_rejects_invalid_delay_before_dispatch(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    for delay in ('-1', 'nan', 'inf'):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(['record', 'check-closed', '--delay', delay, '--json'])
        assert exc_info.value.code == 2
        assert maintenance.check_closed_calls is None
        captured = capsys.readouterr()
        assert captured.out == ''
        assert 'finite non-negative' in captured.err


def test_cli_record_check_closed_recheck_apply_dispatches_recheck_true(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'check-closed', '--recheck-closed', '--apply']) == 0
    assert maintenance.check_closed_calls == [
        {'dry_run': False, 'delay': 1.0, 'platforms': None, 'recheck': True}
    ]
    out = capsys.readouterr().out
    assert out == (
        'mode=reopen apply=true dry_run=false changed=true\n'
        'closed (1):\n- wanted:1\n'
        'reopened (1):\n- wanted:2\n'
        'unknown (0):\n- none\n'
        'skipped (0):\n- none\n'
        'tripped (0):\n- none\n'
    )


def test_cli_record_check_closed_recheck_json_is_exclusive(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        ['record', 'check-closed', '--recheck-closed', '--apply', '--json']
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['mode'] == 'reopen'
    assert payload['apply'] is True
    assert payload['dry_run'] is False
    assert payload['closed_keys'] == ['wanted:1']
    assert payload['reopened_keys'] == ['wanted:2']


def test_cli_record_check_closed_recheck_rejects_unsupported_platform(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(['record', 'check-closed', '--recheck-closed', '--platform', 'nope'])

    assert exit_code != 0
    assert maintenance.check_closed_calls is None
    captured = capsys.readouterr()
    assert captured.out == ''
    stderr = captured.err
    assert 'nope' in stderr
    for platform in cli.SUPPORTED_PLATFORMS:
        assert platform in stderr


@pytest.mark.parametrize('delay', ['-1', 'nan', 'inf'])
def test_cli_record_check_closed_human_rejects_invalid_delay_without_partial_stdout(
    delay, monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(['record', 'check-closed', '--delay', delay])

    assert exc_info.value.code == 2
    assert maintenance.check_closed_calls is None
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'finite non-negative' in captured.err


class _FakeCheckClosedHttpClient:
    def __init__(self, json_queue: list[object]) -> None:
        self.json_queue = list(json_queue)
        self.requests: list[str] = []

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = 'GET',
        body: bytes | None = None,
        error_cls: type[Exception] = Exception,
    ) -> dict:
        self.requests.append(url)
        item = self.json_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, dict)
        return item

    def request_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = 'GET',
        body: bytes | None = None,
        max_bytes: int | None = None,
        error_cls: type[Exception] = Exception,
    ) -> str:
        raise NotImplementedError


def test_cli_record_check_closed_end_to_end_applies_closure(tmp_path: Path, monkeypatch, capsys) -> None:
    from careerkit.jobs.application.maintenance import JobsMaintenanceService
    from careerkit.jobs.domain.model import JobRecord

    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = _FakeCheckClosedHttpClient(json_queue=[{'data': {'job': {'status': 'close'}}}])
    real_bundle = cli._build_services(workspace, http=http)
    maintenance = cast(JobsMaintenanceService, real_bundle.maintenance)
    maintenance.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: real_bundle)

    exit_code = cli.main(['record', 'check-closed', '--apply', '--delay', '0'])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out == (
        'mode=close apply=true dry_run=false changed=true\n'
        'closed (1):\n- wanted:1\n'
        'reopened (0):\n- none\n'
        'unknown (0):\n- none\n'
        'skipped (0):\n- none\n'
        'tripped (0):\n- none\n'
    )
    reloaded = maintenance.repository.get(JobKey('wanted', '1'))
    assert reloaded.record.posting_status is PostingStatus.CLOSED


def test_cli_record_check_closed_json_end_to_end_applies_closure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from careerkit.jobs.application.maintenance import JobsMaintenanceService

    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = _FakeCheckClosedHttpClient(json_queue=[{'data': {'job': {'status': 'close'}}}])
    real_bundle = cli._build_services(workspace, http=http)
    maintenance = cast(JobsMaintenanceService, real_bundle.maintenance)
    maintenance.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: real_bundle)

    exit_code = cli.main(
        ['record', 'check-closed', '--apply', '--delay', '0', '--json']
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['closed_keys'] == ['wanted:1']
    assert payload['reopened_keys'] == []
    assert payload['changed'] is True
    reloaded = maintenance.repository.get(JobKey('wanted', '1'))
    assert reloaded.record.posting_status is PostingStatus.CLOSED


def test_cli_record_check_closed_recheck_end_to_end_restores_reopened(tmp_path: Path, monkeypatch, capsys) -> None:
    from careerkit.jobs.application.maintenance import JobsMaintenanceService
    from careerkit.jobs.domain.model import JobRecord

    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = _FakeCheckClosedHttpClient(json_queue=[{'data': {'job': {'status': 'active'}}}])
    real_bundle = cli._build_services(workspace, http=http)
    maintenance = cast(JobsMaintenanceService, real_bundle.maintenance)
    maintenance.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    maintenance.repository.update_status(JobKey('wanted', '1'), posting_status=PostingStatus.CLOSED)
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: real_bundle)

    exit_code = cli.main(['record', 'check-closed', '--recheck-closed', '--apply', '--delay', '0'])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out == (
        'mode=reopen apply=true dry_run=false changed=true\n'
        'closed (0):\n- none\n'
        'reopened (1):\n- wanted:1\n'
        'unknown (0):\n- none\n'
        'skipped (0):\n- none\n'
        'tripped (0):\n- none\n'
    )
    reloaded = maintenance.repository.get(JobKey('wanted', '1'))
    assert reloaded.record.posting_status is PostingStatus.ACTIVE


def test_cli_record_check_closed_recheck_json_end_to_end_restores_reopened(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from careerkit.jobs.application.maintenance import JobsMaintenanceService

    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    http = _FakeCheckClosedHttpClient(json_queue=[{'data': {'job': {'status': 'active'}}}])
    real_bundle = cli._build_services(workspace, http=http)
    maintenance = cast(JobsMaintenanceService, real_bundle.maintenance)
    maintenance.repository.create(JobRecord('wanted', '1', 'Acme', 'Backend'), jd_markdown='# JD')
    maintenance.repository.update_status(JobKey('wanted', '1'), posting_status=PostingStatus.CLOSED)
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: real_bundle)

    exit_code = cli.main(
        [
            'record',
            'check-closed',
            '--recheck-closed',
            '--apply',
            '--delay',
            '0',
            '--json',
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['closed_keys'] == []
    assert payload['reopened_keys'] == ['wanted:1']
    assert payload['changed'] is True
    reloaded = maintenance.repository.get(JobKey('wanted', '1'))
    assert reloaded.record.posting_status is PostingStatus.ACTIVE


def test_cli_run_auto_forwards_resume_flag(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    automation = FakeAutomation()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=automation,
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['run', 'auto', '--resume', '--json']) == 0
    assert automation.calls == [('auto', ['--resume', '--json'])]
    assert capsys.readouterr().out == '{"status":"ok"}\n'


def test_cli_run_auto_resolves_relative_url_file_against_workspace(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    automation = FakeAutomation()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=automation,
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        ['run', 'auto', '--from-urls', 'private/jd/runtime/search/request.txt', '--json']
    ) == 0
    assert automation.calls == [
        (
            'auto',
            [
                '--from-urls',
                '/workspace/private/jd/runtime/search/request.txt',
                '--json',
            ],
        )
    ]
    assert capsys.readouterr().out == '{"status":"ok"}\n'


def test_cli_run_auto_forwards_operational_limits(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    automation = FakeAutomation()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=automation,
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert (
        cli.main(
            [
                'run',
                'auto',
                '--max-urls',
                '3',
                '--llm-timeout',
                '45',
                '--local-llm-timeout',
                '300',
                '--no-classify',
            ]
        )
        == 0
    )
    assert automation.calls == [
        ('auto', ['--max-urls', '3', '--llm-timeout', '45', '--local-llm-timeout', '300', '--no-classify'])
    ]
    assert capsys.readouterr().out == '{"status":"ok"}\n'


def test_cli_direct_search_persists_returned_seen_keys(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['search', 'run', '--query', 'Backend', '--max-urls', '3', '--json']) == 0
    assert maintenance.search_calls == [(('Backend',), 3)]
    assert maintenance.persisted_seen == [{'wanted:1'}]
    assert json.loads(capsys.readouterr().out)['postings'] == ['wanted:1']


def test_cli_search_dry_run_status_and_reset_preserve_state_controls(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['search', 'run', '--dry-run']) == 0
    assert maintenance.persisted_seen is None
    capsys.readouterr()

    assert cli.main(['search', 'status', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['counts'] == {
        'total_searches': 3,
        'total_new_found': 7,
        'tracked_job_keys': 5,
    }

    assert cli.main(['search', 'reset-state', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['changed'] is True


def test_cli_exposes_closed_backfill_and_stale_screening_report(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['storage', 'backfill-closed', '--apply', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['changed'] is True
    assert payload['job_keys'] == ['wanted:1']

    assert cli.main(['summary', 'stale-screenings', '--days', '45', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['days'] == 45
    assert payload['record_count'] == 2


def test_build_services_wires_real_automation_pipeline(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')

    services = cli._build_services(workspace)

    assert isinstance(services.automation, AutomationService)
    assert services.automation.search_port is services.maintenance
    assert isinstance(services.automation.extraction_stage, JobsExtractionStage)
    assert isinstance(services.automation.screening_stage, JobsScreeningStage)
    assert isinstance(services.automation.completion_stage, JobsCompletionStage)
    assert isinstance(services.automation.resume_state, JobsResumeStateService)


def test_build_services_defers_malformed_screening_config(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    config = tmp_path / 'private/jd/config/search_config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('quick_filters: [unterminated', encoding='utf-8')

    services = cli._build_services(workspace)

    assert isinstance(services.automation, AutomationService)


def test_search_rejects_non_positive_max_urls() -> None:
    parser = cli.build_parser()

    for value in ('0', '-1'):
        try:
            parser.parse_args(['search', 'run', '--max-urls', value])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError('non-positive max urls should fail')


def test_build_services_loads_candidate_context_from_resume_sources(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    profile_dir = tmp_path / 'private/profile'
    company_dir = tmp_path / 'private/companies/acme/projects'
    profile_dir.mkdir(parents=True)
    company_dir.mkdir(parents=True)
    (profile_dir / 'summary-job.md').write_text('# Summary\nBackend evidence', encoding='utf-8')
    (profile_dir / 'contact.md').write_text('private@example.com', encoding='utf-8')
    (company_dir.parent / 'profile.md').write_text('# Acme\nRole evidence', encoding='utf-8')
    (company_dir / 'platform.md').write_text('# Platform\nProject evidence', encoding='utf-8')

    context = load_candidate_context(workspace)
    services = cli._build_services(workspace)

    assert 'Backend evidence' in context
    assert 'Role evidence' in context
    assert 'Project evidence' in context
    assert 'private@example.com' not in context
    automation = cast(AutomationService, services.automation)
    assert isinstance(automation.screening_stage, JobsScreeningStage)
    assert automation.screening_stage.candidate_context == context


def test_cli_screening_validate_reports_json(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    screening_file = tmp_path / 'screening.md'
    screening_file.write_text(
        "## 기본 정보\n\nA\n\n## 스크리닝 결과\n\nB\n\n## 이력/경험 매칭\n\n"
        "| 요건 | 구분 | 대조 | 근거 |\n|---|---|---|---|\n| Backend | 필수 | 충족 | C |\n"
        "\n## 최종 판정\n\n### 최종 판정: 지원 보류\n\n## 핵심 근거\n\nD\nE\n",
        encoding='utf-8',
    )
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['screening', 'validate', str(screening_file), '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['command'] == 'screening validate'
    assert payload['valid'] is True
    assert payload['path'] == str(screening_file)


def test_cli_screening_run_reads_explicit_candidate_context(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    (tmp_path / 'private/jd/records').mkdir(parents=True)
    context_file = tmp_path / 'context.md'
    context_file.write_text('explicit context', encoding='utf-8')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )

    captured: dict[str, object] = {}

    class FakeRepository:
        def get(self, key: JobKey):
            captured['key'] = key
            return SimpleNamespace(record=SimpleNamespace(platform='wanted', job_id='1', company='Acme'), jd_markdown='# JD')

    def fake_run_screening(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            verdict='지원 추천',
            screening_path=Path('wanted/1/screening.md'),
            provider='fake-provider',
            used_fallback=False,
        )

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: FakeRepository())
    monkeypatch.setattr(cli, 'run_screening', fake_run_screening)

    assert cli.main(['screening', 'run', 'wanted:1', '--candidate-context-file', str(context_file), '--dry-run', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['command'] == 'screening run'
    assert payload['job_key'] == 'wanted:1'
    assert payload['verdict'] == '지원 추천'
    assert captured['candidate_context'] == 'explicit context'
    assert captured['repository'] is None
    assert captured['dry_run'] is True


def test_cli_screening_run_loads_workspace_candidate_context_by_default(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    (tmp_path / 'private/jd/records').mkdir(parents=True)
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    captured: dict[str, object] = {}

    class FakeRepository:
        def get(self, key: JobKey):
            return SimpleNamespace(record=SimpleNamespace(platform='wanted', job_id='1', company='Acme'), jd_markdown='# JD')

    def fake_run_screening(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            verdict='지원 보류',
            screening_path=Path('wanted/1/screening.md'),
            provider='fake-provider',
            used_fallback=False,
        )

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: FakeRepository())
    monkeypatch.setattr(cli, 'load_candidate_context', lambda resolved: 'workspace context')
    monkeypatch.setattr(cli, 'run_screening', fake_run_screening)

    assert cli.main(['screening', 'run', 'wanted:1', '--dry-run', '--json']) == 0
    capsys.readouterr()
    assert captured['candidate_context'] == 'workspace context'


def test_cli_screening_runtime_error_is_controlled(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    (tmp_path / 'private/jd/records').mkdir(parents=True)
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )

    class FakeRepository:
        def get(self, key: JobKey):
            return SimpleNamespace(record=SimpleNamespace(company='Acme'), jd_markdown='# JD')

    def fail_screening(**kwargs):
        raise RuntimeError('invalid screening output')

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: FakeRepository())
    monkeypatch.setattr(cli, 'run_screening', fail_screening)

    assert cli.main(['screening', 'run', 'wanted:1']) == 2
    assert 'invalid screening output' in capsys.readouterr().err


def test_cli_config_apply_backup_collision_is_controlled(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(
        cli,
        '_build_services',
        lambda resolved: (_ for _ in ()).throw(FileExistsError('rollback backup already exists')),
    )
    assert cli.main(['config', 'apply']) == 2
    assert 'rollback backup already exists' in capsys.readouterr().err


def test_queue_rescreen_dry_run_uses_company_evidence_and_new_verdict(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')

    class NoClassifyPipeline(FakePipeline):
        def classify_record(self, key: JobKey, *, dry_run: bool = False):
            raise AssertionError('dry-run must not classify stored screening content')

    class FakeRepository:
        def get(self, key: JobKey):
            return SimpleNamespace(
                record=JobRecord('wanted', '1', 'Acme', 'Backend'),
                jd_markdown='# JD',
            )

    company_file = tmp_path / 'private/company_info/acme.md'

    class FakeCompanyInfo:
        def __init__(self, *, workspace):
            pass

        def find_matching_file(self, company_name: str):
            return company_file

        def validate(self, *, file_name: str):
            return SimpleNamespace(errors=(), incomplete_companies=0)

    captured = {}

    def fake_screening(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(verdict='지원 추천')

    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=NoClassifyPipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: FakeRepository())
    monkeypatch.setattr(cli, 'CompanyInfoService', FakeCompanyInfo)
    monkeypatch.setattr(cli, 'run_screening', fake_screening)

    assert cli.main(['queue', 'rescreen', 'wanted:1', '--dry-run', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured['company_file'] == company_file
    assert payload['items'][0]['verdict'] == '지원 추천'


def test_cli_console_serve_uses_loopback_server(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    calls: dict[str, object] = {}

    class FakeServer:
        server_port = 9900

        def serve_forever(self) -> None:
            calls['served'] = True
            raise KeyboardInterrupt

        def server_close(self) -> None:
            calls['closed'] = True

    def fake_create_server(*, records_root, database_path, host, port):
        calls.update(
            {
                'records_root': records_root,
                'database_path': database_path,
                'host': host,
                'port': port,
            }
        )
        return FakeServer()

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'create_server', fake_create_server)

    assert cli.main(['console', 'serve', '--host', '127.0.0.1', '--port', '9900']) == 0
    assert calls['records_root'] == tmp_path / 'private' / 'jd' / 'records'
    assert calls['database_path'] == Path('private/jd/derived') / 'search.sqlite3'
    assert calls['host'] == '127.0.0.1'
    assert calls['port'] == 9900
    assert calls['served'] is True
    assert calls['closed'] is True
    assert 'career-jobs console serving http://127.0.0.1:9900' in capsys.readouterr().out


def test_cli_screening_lint_hook_dispatches_stdin_payload(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    captured: dict[str, object] = {}

    def fake_hook_keys(stdin, *, records_root):
        captured['payload'] = stdin.read()
        captured['records_root'] = records_root
        return [JobKey('wanted', '1')]

    def fake_run(keys, repository):
        captured['keys'] = keys
        captured['repository_root'] = repository.root
        return cli.screening_lint.LintReport(findings=(), keys_checked=1)

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli.screening_lint, 'hook_keys_from_stdin', fake_hook_keys)
    monkeypatch.setattr(cli.screening_lint, 'run', fake_run)
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(stdin=io.StringIO('{"tool_name":"Write"}'), stdout=cli.sys.stdout, stderr=cli.sys.stderr))

    assert cli.main(['screening', 'lint', '--hook']) == 0
    assert captured['payload'] == '{"tool_name":"Write"}'
    assert captured['records_root'] == tmp_path / 'private' / 'jd' / 'records'
    assert captured['keys'] == [JobKey('wanted', '1')]
    assert captured['repository_root'] == tmp_path / 'private' / 'jd' / 'records'
    assert '검사 1건: 위반 0, 경고 0' in capsys.readouterr().out


def test_cli_company_validate_reports_human_summary(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['company', 'validate', '--file', 'acme.md', '--fix']) == 0
    output = capsys.readouterr().out
    assert '기업정보 검증 완료' in output
    assert 'processed=1' in output
    assert 'high=1' in output
    assert 'fixed: acme.md' in output


def test_cli_company_validate_returns_usage_error_when_records_fail(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    maintenance = FakeMaintenance()
    monkeypatch.setattr(
        maintenance,
        'company_validate',
        lambda **_: CompanyValidationSummary(
            processed_files=0,
            error_files=1,
            critical_risk_companies=0,
            high_risk_companies=0,
            incomplete_companies=0,
            results=(),
            errors=('invalid company record',),
            fixed_files=(),
            report_path=None,
        ),
    )
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['company', 'validate']) == 2
    assert 'invalid company record' in capsys.readouterr().err


def test_cli_company_fetch_remember_prints_json(monkeypatch, capsys) -> None:
    import json as _json

    from careerkit.jobs.adapters.platforms import remember as remember_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    fake_info = remember_mod.RememberCompanyInfo(
        company_id=12345,
        name="(주)테스트",
        address="서울 강남구",
        industry="IT·통신 > SW/App",
        established="2023-01-01",
        employee_count=50,
        avg_salary_manwon=5000,
        salary_yoy_change=100,
        employee_stats=({"month": "2026-06", "total": 50, "join": 5, "leave": 3},),
        company_type="스타트업",
        homepage="https://example.com",
        ceo="홍길동",
        tags=("자유복장",),
    )
    monkeypatch.setattr(remember_mod, 'remember_company_http', lambda cid, **kw: fake_info)

    assert cli.main(['company', 'fetch', '--platform', 'remember', '--id', '12345']) == 0
    out = capsys.readouterr().out
    data = _json.loads(out)
    assert data['company_id'] == 12345
    assert data['avg_salary_manwon'] == 5000


def test_cli_company_fetch_remember_error_returns_1(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import remember as remember_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    def _fail(cid, **kw):
        raise ValueError("missing __NEXT_DATA__ payload")

    monkeypatch.setattr(remember_mod, 'remember_company_http', _fail)

    assert cli.main(['company', 'fetch', '--platform', 'remember', '--id', '99999']) == 1
    assert 'error' in capsys.readouterr().err.lower()


def _capped_record(job_id: str, *, capped: bool, provider: str | None = 'ollama') -> JobRecord:
    return JobRecord(
        'wanted',
        job_id,
        'Acme',
        'Backend',
        screening_provider=provider if capped else 'codex',
        verdict_capped=capped,
    )


class _CappedRepository:
    def __init__(self, records: list[JobRecord]) -> None:
        self._records = records
        self.freed: set[str] = set()

    def list_metadata(self):
        return [SimpleNamespace(record=record) for record in self._records]

    def get_metadata(self, key: JobKey):
        for record in self._records:
            if record.job_id == key.job_id:
                if record.job_id in self.freed:
                    return SimpleNamespace(
                        record=JobRecord(
                            'wanted',
                            record.job_id,
                            record.company,
                            record.position,
                            screening_provider='codex',
                            verdict_capped=False,
                        )
                    )
                return SimpleNamespace(record=record)
        raise AssertionError(f'unknown key {key}')

    def get(self, key: JobKey):
        return SimpleNamespace(
            record=JobRecord('wanted', key.job_id, 'Acme', 'Backend'),
            jd_markdown='# JD',
        )


def _capped_cli(monkeypatch, tmp_path: Path, repository: _CappedRepository):
    workspace = WorkspacePaths(root=tmp_path, source='explicit')

    class FakeCompanyInfo:
        def __init__(self, *, workspace):
            pass

        def find_matching_file(self, company_name: str):
            return None

        def validate(self, *, file_name: str):
            return SimpleNamespace(errors=(), incomplete_companies=0)

    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: repository)
    monkeypatch.setattr(cli, 'CompanyInfoService', FakeCompanyInfo)
    monkeypatch.setattr(cli, 'load_candidate_context', lambda workspace: 'context')
    return bundle


def _screening(**overrides):
    """A run_screening double carrying every field the capped path reads.

    Built from one place so a new ScreeningResult field fails once here rather
    than as an AttributeError inside each test's exception handler.
    """
    fields = {
        'verdict': '지원 보류',
        'provider': 'ollama',
        'published': False,
        'used_fallback': False,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_queue_capped_lists_only_capped_records(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _CappedRepository(
        [
            _capped_record('1', capped=True),
            _capped_record('2', capped=False),
            _capped_record('3', capped=True),
            _capped_record('4', capped=False),
            _capped_record('5', capped=False),
        ]
    )
    _capped_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'capped', '--list', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 2
    assert [item['job_key'] for item in payload['items']] == ['wanted:1', 'wanted:3']
    assert payload['items'][0]['screening_provider'] == 'ollama'


def test_queue_capped_lists_nothing_when_no_record_is_capped(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository([_capped_record('1', capped=False)])
    _capped_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'capped', '--list', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 0
    assert payload['items'] == []


def test_queue_capped_respects_limit(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _CappedRepository(
        [_capped_record('1', capped=True), _capped_record('3', capped=True)]
    )
    _capped_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'capped', '--list', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 1


def test_queue_capped_rescreen_leaves_the_record_alone_when_still_capped(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository([_capped_record('1', capped=True)])
    _capped_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli,
        'run_screening',
        lambda **kwargs: _screening(),
    )

    assert cli.main(['queue', 'capped', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['still_capped'] == 1
    assert payload['rescreened'] == 0
    assert payload['items'][0]['outcome'] == 'still_capped'
    assert 'left untouched' in payload['items'][0]['message']


def test_queue_capped_rescreen_reports_a_fallback_document_as_unrecovered(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository([_capped_record('1', capped=True)])
    _capped_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli,
        'run_screening',
        lambda **kwargs: _screening(provider='fallback', used_fallback=True),
    )

    assert cli.main(['queue', 'capped', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['still_capped'] == 1
    assert payload['rescreened'] == 0
    # Nothing capped it — no provider answered at all.
    assert 'no provider answered' in payload['items'][0]['message']


def test_queue_capped_rescreen_counts_a_protected_record_as_recovered(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    """A protected status classifies as skipped even after the cap is lifted."""
    repository = _CappedRepository([_capped_record('1', capped=True)])
    repository.freed.add('1')
    bundle = _capped_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        bundle.pipeline,
        'classify_record',
        lambda key, *, dry_run=False: IngestResult(
            source='wanted/1', job_id='1', outcome='skipped', message='status is protected'
        ),
    )
    monkeypatch.setattr(
        cli,
        'run_screening',
        lambda **kwargs: _screening(
            verdict='지원 추천', provider='codex', published=True
        ),
    )

    assert cli.main(['queue', 'capped', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['rescreened'] == 1
    assert payload['still_capped'] == 0
    assert payload['items'][0]['outcome'] == 'rescreened'
    assert payload['items'][0]['classification'] == 'skipped'


def test_queue_capped_rescreen_requires_a_strong_provider(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository([_capped_record('1', capped=True)])
    _capped_cli(monkeypatch, tmp_path, repository)
    captured = {}

    def fake_screening(**kwargs):
        captured.update(kwargs)
        return _screening()

    monkeypatch.setattr(cli, 'run_screening', fake_screening)

    cli.main(['queue', 'capped', '--rescreen', '--json'])

    assert captured['require_strong_provider'] is True
    assert captured['dry_run'] is False


def test_queue_capped_rescreen_reports_records_freed_by_a_strong_provider(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository([_capped_record('1', capped=True)])
    repository.freed.add('1')
    _capped_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli,
        'run_screening',
        lambda **kwargs: _screening(
            verdict='지원 추천', provider='codex', published=True
        ),
    )

    assert cli.main(['queue', 'capped', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['still_capped'] == 0
    assert payload['rescreened'] == 1
    assert payload['items'][0]['outcome'] == 'rescreened'


def test_queue_capped_rescreen_isolates_a_failing_record(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    repository = _CappedRepository(
        [_capped_record('1', capped=True), _capped_record('3', capped=True)]
    )
    repository.freed.add('3')  # the strong provider republishes this one
    _capped_cli(monkeypatch, tmp_path, repository)
    seen: list[str] = []

    def flaky(**kwargs):
        job_id = kwargs['jd'].record.job_id
        seen.append(job_id)
        if job_id == '1':
            raise ValueError('company info is invalid or incomplete')
        return _screening(verdict='지원 추천', provider='codex', published=True)

    monkeypatch.setattr(cli, 'run_screening', flaky)

    assert cli.main(['queue', 'capped', '--rescreen', '--json']) == 2

    payload = json.loads(capsys.readouterr().out)
    assert seen == ['1', '3']  # the batch continued past the failure
    assert payload['failed'] == 1
    assert payload['rescreened'] == 1


def test_queue_capped_rejects_a_negative_limit(monkeypatch, tmp_path: Path) -> None:
    """capped[:-1] would rescreen every record but the last, invoking providers
    and republishing revisions across nearly the whole queue."""
    repository = _CappedRepository([_capped_record('1', capped=True)])
    _capped_cli(monkeypatch, tmp_path, repository)

    with pytest.raises(SystemExit):
        cli.main(['queue', 'capped', '--rescreen', '--limit', '-1'])


def test_queue_capped_rejects_list_and_rescreen_together(monkeypatch, tmp_path: Path) -> None:
    repository = _CappedRepository([_capped_record('1', capped=True)])
    _capped_cli(monkeypatch, tmp_path, repository)

    with pytest.raises(SystemExit):
        cli.main(['queue', 'capped', '--list', '--rescreen'])


# ---------------------------------------------------------------------------
# queue fallback
# ---------------------------------------------------------------------------

_FALLBACK_DOC = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 파일 | wanted/99 |
| 회사명 | Acme |
| 포지션 | Backend |
| 출처 | https://example.com |
| 생성 방식 | 자동 fallback |

## 스크리닝 결과

LLM 스크리닝 실행이 완료되지 않아 자동 판정은 보류로 기록한다. 채용 적합성은 수동 재스크리닝 전까지 확정하지 않는다.

## 이력/경험 매칭

| 항목 | 판단 |
|------|------|
| 후보자 이력 대조 | LLM 분석 실패로 이력 근거 대조가 수행되지 않았다. |
| JD 필수요건 대조 | 수동 재스크리닝 필요. |

## 최종 판정

### 최종 판정: 지원 보류

## 핵심 근거

- 자동 분석 경로에서 LLM 응답을 얻지 못했다.
- 실패 사유: timeout
- 이 문서는 원시 실행 로그를 저장하지 않고 수동 재스크리닝을 위한 보류 상태만 기록한다.
"""

_REAL_SCREENING_DOC = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | RealCo |

## 스크리닝 결과

실질 분석.

## 이력/경험 매칭

| 항목 | 충족 여부 | 근거 | 출처 |
|------|-----------|------|------|
| Python | ⭕ 충족 | 이력서 | [resume] |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 충족.
"""


def _fallback_record(
    job_id: str,
    *,
    screening_md: str | None = None,
    posting_status: PostingStatus = PostingStatus.ACTIVE,
) -> JobRecord:
    return JobRecord(
        'wanted', job_id, 'Acme', 'Backend',
        screening_verdict=ScreeningVerdict.HOLD,
        posting_status=posting_status,
    )


class _FallbackRepository:
    def __init__(self, records: list[tuple[JobRecord, str | None]]) -> None:
        self._records = records
        self._screening_overrides: dict[str, str] = {}

    def list_metadata(self):
        return [SimpleNamespace(record=r, has_screening=md is not None) for r, md in self._records]

    def get_metadata(self, key: JobKey):
        for r, _md in self._records:
            if r.job_id == key.job_id:
                return SimpleNamespace(record=r)
        raise AssertionError(f'unknown key {key}')

    def get(self, key: JobKey):
        for r, md in self._records:
            if r.job_id == key.job_id:
                override = self._screening_overrides.get(key.job_id)
                return SimpleNamespace(
                    record=r,
                    jd_markdown='# JD',
                    screening_markdown=override if override is not None else md,
                )
        raise AssertionError(f'unknown key {key}')

    def set_screening_override(self, job_id: str, md: str) -> None:
        self._screening_overrides[job_id] = md


def _fallback_cli(monkeypatch, tmp_path: Path, repository: _FallbackRepository):
    workspace = WorkspacePaths(root=tmp_path, source='explicit')

    class FakeCompanyInfo:
        def __init__(self, *, workspace):
            pass

        def find_matching_file(self, company_name: str):
            return None

        def validate(self, *, file_name: str):
            return SimpleNamespace(errors=(), incomplete_companies=0)

    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: repository)
    monkeypatch.setattr(cli, 'CompanyInfoService', FakeCompanyInfo)
    monkeypatch.setattr(cli, 'load_candidate_context', lambda workspace: 'context')
    return bundle


def test_queue_fallback_lists_only_fallback_documents(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([
        (_fallback_record('1'), _FALLBACK_DOC),
        (_fallback_record('2'), _REAL_SCREENING_DOC),
        (_fallback_record('3'), _FALLBACK_DOC),
        (_fallback_record('4'), None),
    ])
    _fallback_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'fallback', '--list', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 2
    assert [item['job_key'] for item in payload['items']] == ['wanted:1', 'wanted:3']


def test_queue_fallback_skips_closed_by_default(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([
        (_fallback_record('1'), _FALLBACK_DOC),
        (_fallback_record('2', posting_status=PostingStatus.CLOSED), _FALLBACK_DOC),
    ])
    _fallback_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'fallback', '--list', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 1
    assert payload['skipped_closed'] == 1
    assert payload['items'][0]['job_key'] == 'wanted:1'


def test_queue_fallback_include_closed(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([
        (_fallback_record('1'), _FALLBACK_DOC),
        (_fallback_record('2', posting_status=PostingStatus.CLOSED), _FALLBACK_DOC),
    ])
    _fallback_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'fallback', '--list', '--include-closed', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 2
    assert payload['skipped_closed'] == 0


def test_queue_fallback_limit_applies_after_closed_filter(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([
        (_fallback_record('1', posting_status=PostingStatus.CLOSED), _FALLBACK_DOC),
        (_fallback_record('2'), _FALLBACK_DOC),
        (_fallback_record('3'), _FALLBACK_DOC),
    ])
    _fallback_cli(monkeypatch, tmp_path, repository)

    assert cli.main(['queue', 'fallback', '--list', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['count'] == 1
    assert payload['skipped_closed'] == 1
    assert payload['items'][0]['job_key'] == 'wanted:2'


def test_queue_fallback_rescreen_aborts_without_strong_provider(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([(_fallback_record('1'), _FALLBACK_DOC)])
    _fallback_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli, 'resolve_commands',
        lambda environment=None: [],
    )

    assert cli.main(['queue', 'fallback', '--rescreen', '--json']) == 1

    payload = json.loads(capsys.readouterr().out)
    assert 'error' in payload


def test_queue_fallback_rescreen_recovers(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([(_fallback_record('1'), _FALLBACK_DOC)])
    _fallback_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli, 'resolve_commands',
        lambda environment=None: [('claude', ['claude', '--print'])],
    )

    def fake_run_screening(**kwargs):
        repository.set_screening_override('1', _REAL_SCREENING_DOC)
        return _screening(provider='claude', published=True)

    monkeypatch.setattr(cli, 'run_screening', fake_run_screening)

    assert cli.main(['queue', 'fallback', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['rescreened'] == 1
    assert payload['still_fallback'] == 0


def test_queue_fallback_rescreen_still_fallback_when_no_strong(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([(_fallback_record('1'), _FALLBACK_DOC)])
    _fallback_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli, 'resolve_commands',
        lambda environment=None: [('claude', ['claude', '--print'])],
    )
    monkeypatch.setattr(
        cli, 'run_screening',
        lambda **kwargs: _screening(provider='ollama', published=False, used_fallback=False),
    )

    assert cli.main(['queue', 'fallback', '--rescreen', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['still_fallback'] == 1
    assert payload['rescreened'] == 0


def test_queue_fallback_rescreen_requires_strong_provider(monkeypatch, capsys, tmp_path: Path) -> None:
    repository = _FallbackRepository([(_fallback_record('1'), _FALLBACK_DOC)])
    _fallback_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli, 'resolve_commands',
        lambda environment=None: [('claude', ['claude', '--print'])],
    )
    captured: dict[str, Any] = {}

    def capture_screening(**kwargs):
        captured['require_strong_provider'] = kwargs.get('require_strong_provider')
        return _screening(provider='ollama', published=False)

    monkeypatch.setattr(cli, 'run_screening', capture_screening)

    cli.main(['queue', 'fallback', '--rescreen', '--json'])

    assert captured['require_strong_provider'] is True


def test_queue_fallback_rescreen_failed_after_publish(monkeypatch, capsys, tmp_path: Path) -> None:
    """A strong provider publishes, then classify_record throws."""
    repository = _FallbackRepository([(_fallback_record('1'), _FALLBACK_DOC)])
    bundle = _fallback_cli(monkeypatch, tmp_path, repository)
    monkeypatch.setattr(
        cli, 'resolve_commands',
        lambda environment=None: [('claude', ['claude', '--print'])],
    )

    def fake_run_screening(**kwargs):
        repository.set_screening_override('1', _REAL_SCREENING_DOC)
        return _screening(provider='claude', published=True)

    monkeypatch.setattr(cli, 'run_screening', fake_run_screening)
    cast(FakePipeline, bundle.pipeline).classify_error = RuntimeError("IO error")

    assert cli.main(['queue', 'fallback', '--rescreen', '--json']) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload['failed_after_publish'] == 1
    assert payload['failed'] == 0
