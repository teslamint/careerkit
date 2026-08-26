from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
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
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.company_info import CompanyValidationSummary
from careerkit.jobs.application.maintenance import CheckClosedResult
from careerkit.jobs.application.pipeline import IngestResult, PrescreenedListing, QueueStatusResult
from careerkit.jobs.application.preflight import PreflightFinding, StoragePreflightResult
from careerkit.jobs.domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.workspace import WorkspacePaths


@dataclass
class FakeMaintenance:
    persisted_seen: list[set[str]] | None = None
    cleaned_preflights: list[Path] | None = None
    search_calls: list[tuple[tuple[str, ...] | None, int | None]] | None = None
    check_closed_calls: list[dict[str, object]] | None = None
    semantic_eval_capture_calls: list[dict[str, object]] | None = None
    semantic_eval_run_calls: list[dict[str, object]] | None = None
    semantic_eval_compare_calls: list[dict[str, object]] | None = None
    semantic_eval_run_error: Exception | None = None

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

    def check_closed(self, *, dry_run=True, delay=1.0, platforms=None, recheck=False, keys=None):
        from careerkit.jobs.application.maintenance import CheckClosedResult
        if self.check_closed_calls is None:
            self.check_closed_calls = []
        self.check_closed_calls.append(
            {'dry_run': dry_run, 'delay': delay, 'platforms': platforms, 'recheck': recheck, 'keys': keys}
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
        return StoragePreflightResult(
            ready=True,
            record_count=1,
            screening_count=1,
            checked_keys=('wanted:1',),
            schema_version=2,
            isolated_output_root=Path('tmp/preflight'),
            findings=(PreflightFinding('ok', 'clear', 'wanted:1'),),
            status_counts={'records:total': 1},
            application_timestamp_categories={
                'absent': 1,
                'aware': 2,
                'invalid': 3,
                'naive': 4,
            },
        )

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

    def semantic_eval_capture(self, *, output_path: Path, seed: int | None = None):
        if self.semantic_eval_capture_calls is None:
            self.semantic_eval_capture_calls = []
        self.semantic_eval_capture_calls.append({'output_path': output_path, 'seed': seed})
        return SimpleNamespace(
            output_path=output_path,
            status='ok',
            error_code=None,
            aggregate={
                'status': 'ok',
                'error_code': None,
                'counts': {'captured_cases': 1},
                'capture_provenance': {'platforms': {'wanted': {'complete': True}}},
            },
            private_titles=('SENTINEL PRIVATE BACKEND TITLE',),
            private_case_ids=('case-private-1',),
        )

    def semantic_eval_run(self, *, dataset_path: Path, output_path: Path):
        if self.semantic_eval_run_error is not None:
            raise self.semantic_eval_run_error
        if self.semantic_eval_run_calls is None:
            self.semantic_eval_run_calls = []
        self.semantic_eval_run_calls.append({'dataset_path': dataset_path, 'output_path': output_path})
        return SimpleNamespace(
            output_path=output_path,
            status='insufficient_data',
            error_code='insufficient_data',
            aggregate={
                'status': 'insufficient_data',
                'authorizes_production_change': False,
                'counts': {'holdout_cases': 1},
                'comparison': None,
            },
            private_titles=('SENTINEL PRIVATE BACKEND TITLE',),
            private_case_ids=('case-private-1',),
        )

    def semantic_eval_compare(self, *, dataset_path: Path, incumbent_path: Path, candidate_path: Path, output_path: Path | None = None):
        if self.semantic_eval_compare_calls is None:
            self.semantic_eval_compare_calls = []
        self.semantic_eval_compare_calls.append({
            'dataset_path': dataset_path,
            'incumbent_path': incumbent_path,
            'candidate_path': candidate_path,
            'output_path': output_path,
        })
        return SimpleNamespace(
            output_path=output_path,
            status='fail',
            error_code='candidate_not_authorized',
            aggregate={
                'status': 'fail',
                'authorizes_production_change': False,
                'comparison': {'reason': 'candidate_not_authorized', 'candidate_gains': 0, 'candidate_losses': 0, 'mcnemar_p_value': 1.0},
            },
            private_titles=('SENTINEL PRIVATE BACKEND TITLE',),
            private_case_ids=('case-private-1',),
        )

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
    repository: JDRecordRepository = field(
        default_factory=lambda: JDRecordRepository(Path('/tmp/fake-cli-records'))
    )

    def ingest_url(self, url: str) -> IngestResult:
        return IngestResult(source=url, job_id='1', outcome='needs_manual', message='extract me')

    def ingest_file(self, path: Path):
        return [IngestResult(source=str(path), job_id='1', outcome='needs_manual', message='extract me')]

    def show_record(self, key: JobKey):
        from careerkit.jobs.adapters.storage.file_records import StoredJobMetadata
        return StoredJobMetadata(
            record=JobRecord(
                'wanted',
                '1',
                'Acme',
                'Backend',
                screening_verdict=ScreeningVerdict.HOLD,
                application_status=ApplicationStatus.INTERVIEW,
                application_status_updated_at='2026-07-14T11:00:00+09:00',
                application_history=(
                    ApplicationEvent(
                        status=ApplicationStatus.APPLIED,
                        occurred_at='2026-07-13T09:00:00+09:00',
                        note='지원서 제출',
                    ),
                    ApplicationEvent(
                        status=ApplicationStatus.INTERVIEW,
                        occurred_at='2026-07-14T11:00:00+09:00',
                        note='1차 기술 면접',
                    ),
                ),
            ),
            has_screening=True,
        )

    def set_record_status(self, key: JobKey, **kwargs):
        from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
        if self.status_calls is None:
            self.status_calls = []
        self.status_calls.append(kwargs)
        note = cast(str | None, kwargs.get('application_note'))
        occurred_at = cast(str | None, kwargs.get('application_status_updated_at')) or '2026-07-14T11:00:00+09:00'
        return StoredJobRecord(
            record=JobRecord(
                'wanted',
                '1',
                'Acme',
                'Backend',
                application_status=ApplicationStatus.APPLIED,
                posting_status=PostingStatus.ACTIVE,
                application_status_updated_at=occurred_at,
                application_history=(
                    ApplicationEvent(
                        status=ApplicationStatus.APPLIED,
                        occurred_at=occurred_at,
                        note=note,
                    ),
                ),
            ),
            jd_markdown='# JD',
            screening_markdown=None,
        )

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

    def list_prescreened(self, *, reason: str | None = None) -> PrescreenedListing:
        # Present so this double still satisfies PipelineOps, but never a stand-in
        # for _PrescreenedPipeline: an empty listing would let a queue prescreened
        # test pass while asserting nothing.
        raise AssertionError('queue prescreened tests use _PrescreenedPipeline')

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
    assert payload['application_timestamp_categories'] == {
        'absent': 1,
        'aware': 2,
        'invalid': 3,
        'naive': 4,
    }
    assert 'wanted:1' not in storage_output
    assert 'checked_keys' not in payload
    assert 'isolated_output_root' not in payload
    assert maintenance.cleaned_preflights == [Path('tmp/preflight')]

    exit_code = cli.main(['summary', 'rebuild', '--json'])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload['output_path'] == 'private/jd/derived/screening-summary.md'





def test_semantic_eval_parser_requires_integer_seed_and_exact_shapes() -> None:
    parser = cli.build_parser()

    capture = parser.parse_args(['semantic-eval', 'capture', '--output', 'private/jd/runtime/semantic-eval/queue.json', '--seed', '17', '--json'])
    assert capture.command == 'semantic-eval'
    assert capture.semantic_eval_command == 'capture'
    assert capture.seed == 17

    run = parser.parse_args(['semantic-eval', 'run', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--output', 'private/jd/evals/semantic-filter/reports/incumbent.json', '--json'])
    assert run.semantic_eval_command == 'run'
    assert run.dataset == Path('private/jd/evals/semantic-filter/gold.json')

    compare = parser.parse_args(['semantic-eval', 'compare', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--incumbent', 'private/jd/evals/semantic-filter/reports/incumbent.json', '--candidate', 'private/jd/evals/semantic-filter/reports/candidate.json', '--output', 'private/jd/evals/semantic-filter/reports/compare.json', '--json'])
    assert compare.semantic_eval_command == 'compare'
    assert compare.output == Path('private/jd/evals/semantic-filter/reports/compare.json')

    with pytest.raises(SystemExit):
        parser.parse_args(['semantic-eval', 'capture', '--output', 'private/jd/runtime/semantic-eval/queue.json', '--seed', 'nope', '--json'])


def test_cli_semantic_eval_json_dispatches_stable_exit_codes_and_redacts_private_fields(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(maintenance=maintenance, pipeline=FakePipeline(), automation=FakeAutomation())
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['semantic-eval', 'capture', '--output', 'private/jd/runtime/semantic-eval/queue.json', '--json']) == 0
    capture_payload = json.loads(capsys.readouterr().out)
    assert capture_payload['command'] == 'semantic-eval capture'
    assert capture_payload['status'] == 'ok'
    assert capture_payload['error_code'] is None
    assert 'SENTINEL PRIVATE BACKEND TITLE' not in json.dumps(capture_payload, ensure_ascii=False)
    assert 'case-private-1' not in json.dumps(capture_payload, ensure_ascii=False)

    assert cli.main(['semantic-eval', 'run', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--output', 'private/jd/evals/semantic-filter/reports/incumbent.json', '--json']) == 2
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload['status'] == 'insufficient_data'
    assert run_payload['error_code'] == 'insufficient_data'
    serialized = json.dumps(run_payload, ensure_ascii=False)
    assert 'SENTINEL PRIVATE BACKEND TITLE' not in serialized
    assert 'case-private-1' not in serialized

    assert cli.main(['semantic-eval', 'compare', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--incumbent', 'private/jd/evals/semantic-filter/reports/incumbent.json', '--candidate', 'private/jd/evals/semantic-filter/reports/candidate.json', '--json']) == 2
    compare_payload = json.loads(capsys.readouterr().out)
    assert compare_payload['status'] == 'fail'
    assert compare_payload['error_code'] == 'candidate_not_authorized'
    assert 'comparison' in compare_payload
    assert 'SENTINEL PRIVATE BACKEND TITLE' not in json.dumps(compare_payload, ensure_ascii=False)
    assert 'case-private-1' not in json.dumps(compare_payload, ensure_ascii=False)


def test_cli_semantic_eval_json_rejects_wrong_artifact_class_roots(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    maintenance = FakeMaintenance()
    maintenance.semantic_eval_run_error = ValueError('semantic eval output path must stay inside the synthetic temp root')
    bundle = cli.ServiceBundle(maintenance=maintenance, pipeline=FakePipeline(), automation=FakeAutomation())
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['semantic-eval', 'run', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--output', 'private/jd/derived/not-allowed.json', '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'error'
    assert payload['error_code'] == 'unsafe_output_path'


def test_cli_semantic_eval_json_returns_stable_error_codes_for_validation_failures(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    maintenance = FakeMaintenance()
    maintenance.semantic_eval_run_error = ValueError('semantic eval output must not target a tracked git path')
    bundle = cli.ServiceBundle(maintenance=maintenance, pipeline=FakePipeline(), automation=FakeAutomation())
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['semantic-eval', 'run', '--dataset', 'private/jd/evals/semantic-filter/gold.json', '--output', 'private/jd/evals/semantic-filter/reports/incumbent.json', '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['status'] == 'error'
    assert payload['error_code'] == 'unsafe_output_path'
    assert 'tracked git path' not in capsys.readouterr().err

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


def test_cli_record_status_leaves_default_timestamp_to_repository(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    pipeline = FakePipeline()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        ['record', 'set-status', 'wanted:1', '--application-status', 'applied', '--json']
    ) == 0
    capsys.readouterr()
    assert pipeline.status_calls == [
        {
            'application_status': ApplicationStatus.APPLIED,
            'posting_status': None,
            'application_status_updated_at': None,
            'application_note': None,
        }
    ]


def test_cli_record_status_json_includes_note_and_history(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    pipeline = FakePipeline()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status',
            'applied',
            '--application-note',
            '지원서 제출',
            '--json',
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert pipeline.status_calls == [
        {
            'application_status': ApplicationStatus.APPLIED,
            'posting_status': None,
            'application_status_updated_at': None,
            'application_note': '지원서 제출',
        }
    ]
    assert payload['application_history'] == [
        {
            'status': 'applied',
            'occurred_at': '2026-07-14T11:00:00+09:00',
            'note': '지원서 제출',
        }
    ]


def test_cli_record_show_outputs_history_in_json_and_human_modes(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'show', 'wanted:1', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['application_history'] == [
        {
            'status': 'applied',
            'occurred_at': '2026-07-13T09:00:00+09:00',
            'note': '지원서 제출',
        },
        {
            'status': 'interview',
            'occurred_at': '2026-07-14T11:00:00+09:00',
            'note': '1차 기술 면접',
        },
    ]

    assert payload['prescreen_reason'] is None

    assert cli.main(['record', 'show', 'wanted:1']) == 0
    assert capsys.readouterr().out == (
        'job_key=wanted:1\n'
        'has_screening=True\n'
        'screening_verdict=hold\n'
        'prescreen_reason=None\n'
        'application_status=interview\n'
        'posting_status=active\n'
        'application_status_updated_at=2026-07-14T11:00:00+09:00\n'
        'schema_version=2\n'
        'application_history[1]=2026-07-13T09:00:00+09:00 applied note=지원서 제출\n'
        'application_history[2]=2026-07-14T11:00:00+09:00 interview note=1차 기술 면접\n'
    )


def test_cli_record_status_rejects_note_without_application_status_before_service_call(
    monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    pipeline = FakePipeline()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        ['record', 'set-status', 'wanted:1', '--application-note', '지원서 제출']
    ) == 2

    assert pipeline.status_calls is None
    assert 'application status is required when application_note is set' in capsys.readouterr().err


def test_cli_record_status_rejects_timestamp_without_application_status_before_service_call(
    monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    pipeline = FakePipeline()
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status-updated-at',
            '2026-08-10T09:30:00+09:00',
        ]
    ) == 2

    assert pipeline.status_calls is None
    assert 'application status is required when application_status_updated_at is set' in capsys.readouterr().err


def test_cli_record_show_json_synthesizes_history_for_legacy_v1_records(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli._build_services(workspace)
    maintenance = cast(JobsMaintenanceService, bundle.maintenance)
    repository = maintenance.repository
    repository.create(
        JobRecord(
            'wanted',
            '1',
            'Acme',
            'Backend',
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at='2026-07-13T09:00:00+09:00',
            schema_version=1,
        ),
        jd_markdown='# JD',
    )
    manifest_path = tmp_path / 'private' / 'jd' / 'records' / 'wanted' / '1' / 'record.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['schema_version'] = 1
    manifest['record']['schema_version'] = 1
    manifest['record'].pop('application_history', None)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'show', 'wanted:1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['schema_version'] == 2
    assert payload['application_history'] == [
        {
            'status': 'applied',
            'occurred_at': '2026-07-13T09:00:00+09:00',
            'note': None,
        }
    ]


def test_cli_record_status_preserves_repeated_and_corrective_history_end_to_end(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli._build_services(workspace)
    maintenance = cast(JobsMaintenanceService, bundle.maintenance)
    maintenance.repository.create(
        JobRecord('wanted', '1', 'Acme', 'Backend'),
        jd_markdown='# JD',
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    commands = [
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status',
            'applied',
            '--application-note',
            '지원서 제출',
        ],
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status',
            'interview',
            '--application-note',
            '1차 기술 면접',
        ],
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status',
            'interview',
            '--application-note',
            '2차 기술 면접',
        ],
        [
            'record',
            'set-status',
            'wanted:1',
            '--application-status',
            'rejected',
            '--application-note',
            '최종 결과 수신',
        ],
    ]
    for command in commands:
        assert cli.main(command) == 0
        capsys.readouterr()

    assert cli.main(['record', 'show', 'wanted:1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [
        (item['status'], item['note'])
        for item in payload['application_history']
    ] == [
        ('applied', '지원서 제출'),
        ('interview', '1차 기술 면접'),
        ('interview', '2차 기술 면접'),
        ('rejected', '최종 결과 수신'),
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
        {'dry_run': True, 'delay': 1.0, 'platforms': None, 'recheck': False, 'keys': None}
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
        {'dry_run': False, 'delay': 1.0, 'platforms': ('wanted',), 'recheck': False, 'keys': None}
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
        'keys': None,
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
            'keys': None,
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
        {'dry_run': False, 'delay': 1.0, 'platforms': None, 'recheck': True, 'keys': None}
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


def test_cli_record_check_closed_individual_keys(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'check-closed', 'wanted:123', 'remember:456']) == 0
    assert maintenance.check_closed_calls == [
        {
            'dry_run': True, 'delay': 1.0, 'platforms': None, 'recheck': False,
            'keys': (JobKey('wanted', '123'), JobKey('remember', '456')),
        }
    ]


def test_cli_record_check_closed_individual_keys_with_apply(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['record', 'check-closed', 'wanted:123', '--apply']) == 0
    assert maintenance.check_closed_calls == [
        {
            'dry_run': False, 'delay': 1.0, 'platforms': None, 'recheck': False,
            'keys': (JobKey('wanted', '123'),),
        }
    ]


def test_cli_record_check_closed_keys_and_platform_raises(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(
        ['record', 'check-closed', 'wanted:123', '--platform', 'wanted']
    )
    assert exit_code != 0
    assert 'mutually exclusive' in capsys.readouterr().err


def test_cli_record_check_closed_invalid_key_format(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(['record', 'check-closed', 'badformat'])
    assert exit_code != 0
    assert 'job_key must be platform:job_id' in capsys.readouterr().err


def test_cli_record_check_closed_unsupported_key_platform(monkeypatch, capsys) -> None:
    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    maintenance = FakeMaintenance()
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    exit_code = cli.main(['record', 'check-closed', 'nope:123'])
    assert exit_code != 0
    assert 'nope' in capsys.readouterr().err


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


def test_cli_screening_lint_file_reports_screening_structure(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    records_root = tmp_path / 'private/jd/records'
    repository = JDRecordRepository(records_root)
    record = JobRecord(platform='wanted', job_id='1', company='Acme', position='Backend', screening_verdict=ScreeningVerdict.RECOMMENDED)
    repository.create(record, jd_markdown='# JD')
    repository.update_screening_result(record.key, screening_markdown='## 기본 정보\n\nA\n')
    screen_path = next((records_root / 'wanted' / '1' / 'content').glob('*/screening.md'))
    bundle = cli.ServiceBundle(maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation())
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['screening', 'lint', '--file', str(screen_path), '--json']) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['findings'][0]['check'] == 'screening-structure'


def test_cli_screening_lint_hook_reports_structure_before_hash_integrity(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    records_root = tmp_path / 'private/jd/records'
    repository = JDRecordRepository(records_root)
    record = JobRecord(platform='wanted', job_id='1', company='Acme', position='Backend', screening_verdict=ScreeningVerdict.RECOMMENDED)
    repository.create(record, jd_markdown='# JD')
    repository.update_screening_result(record.key, screening_markdown='## 기본 정보\n\nA\n')
    screen_path = next((records_root / 'wanted' / '1' / 'content').glob('*/screening.md'))
    bundle = cli.ServiceBundle(maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation())
    payload = {
        'cwd': str(tmp_path),
        'tool_name': 'Write',
        'tool_input': {'file_path': str(screen_path.relative_to(tmp_path))},
    }
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(
        cli,
        'sys',
        SimpleNamespace(stdin=io.StringIO(json.dumps(payload)), stdout=cli.sys.stdout, stderr=cli.sys.stderr),
    )

    assert cli.main(['screening', 'lint', '--hook', '--json']) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['findings'][0]['check'] == 'screening-structure'


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
            fallback_reason=None,
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
            fallback_reason=None,
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

    def fake_create_server(*, records_root, database_path, pipeline_service, host, port):
        calls.update(
            {
                'records_root': records_root,
                'database_path': database_path,
                'pipeline_service': pipeline_service,
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
    assert calls['pipeline_service'] is bundle.pipeline
    assert calls['host'] == '127.0.0.1'
    assert calls['port'] == 9900
    assert calls['served'] is True
    assert calls['closed'] is True
    assert 'career-jobs console serving http://127.0.0.1:9900' in capsys.readouterr().out


def test_cli_screening_lint_hook_tolerates_empty_stdin(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(
        cli.screening_lint,
        'hook_target_paths',
        lambda *, payload, records_root: (_ for _ in ()).throw(AssertionError('should not inspect empty stdin')),
    )
    monkeypatch.setattr(
        cli,
        'sys',
        SimpleNamespace(stdin=io.StringIO(''), stdout=cli.sys.stdout, stderr=cli.sys.stderr),
    )

    assert cli.main(['screening', 'lint', '--hook']) == 0
    assert '검사 0건: 위반 0, 경고 0' in capsys.readouterr().out


def test_cli_screening_lint_hook_tolerates_malformed_json(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(
        cli.screening_lint,
        'hook_target_paths',
        lambda *, payload, records_root: (_ for _ in ()).throw(AssertionError('should not inspect malformed stdin')),
    )
    monkeypatch.setattr(
        cli,
        'sys',
        SimpleNamespace(stdin=io.StringIO('{bad json'), stdout=cli.sys.stdout, stderr=cli.sys.stderr),
    )

    assert cli.main(['screening', 'lint', '--hook']) == 0
    assert '검사 0건: 위반 0, 경고 0' in capsys.readouterr().out


def test_cli_screening_lint_hook_dispatches_stdin_payload(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    captured: dict[str, object] = {}

    def fake_hook_targets(*, payload, records_root):
        captured['payload'] = payload
        captured['records_root'] = records_root
        return [tmp_path / 'private' / 'jd' / 'records' / 'wanted' / '1' / 'content' / 'rev' / 'screening.md']

    def fake_lint_path(path, repository):
        captured['path'] = path
        captured['repository_root'] = repository.root
        return []

    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli.screening_lint, 'hook_target_paths', fake_hook_targets)
    monkeypatch.setattr(cli.screening_lint, 'lint_path', fake_lint_path)
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(stdin=io.StringIO('{"tool_name":"Write"}'), stdout=cli.sys.stdout, stderr=cli.sys.stderr))

    assert cli.main(['screening', 'lint', '--hook']) == 0
    assert captured['payload'] == {'tool_name': 'Write'}
    assert captured['records_root'] == tmp_path / 'private' / 'jd' / 'records'
    assert captured['path'] == tmp_path / 'private' / 'jd' / 'records' / 'wanted' / '1' / 'content' / 'rev' / 'screening.md'
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

    assert cli.main(['company', 'fetch', '--platform', 'remember', '--id', '12345', '--json']) == 0
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


def test_cli_company_fetch_wanted_prints_markdown(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name='테스트',
        industry='Software',
        founded_year=2019,
        location='서울',
        employee_count=42,
        avg_salary_manwon=6000,
        hired_1y=5,
        left_1y=2,
        total_sales_eok=10.5,
        sales_year='2025',
        tags=('AI', 'Remote'),
        description='소개',
        homepage='https://example.com',
    )
    monkeypatch.setattr(wanted_mod, 'wanted_company_http', lambda cid, **kw: fake_info)

    assert cli.main(['company', 'fetch', '--platform', 'wanted', '--id', '12345']) == 0
    out = capsys.readouterr().out
    assert out == wanted_mod.format_wanted_company_markdown(fake_info) + '\n'


def test_cli_company_fetch_wanted_prints_json_with_list_tags(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name='테스트',
        industry='Software',
        founded_year=2019,
        location='서울',
        employee_count=42,
        avg_salary_manwon=6000,
        hired_1y=5,
        left_1y=2,
        total_sales_eok=10.5,
        sales_year='2025',
        tags=('AI', 'Remote'),
        description='소개',
        homepage='https://example.com',
    )
    monkeypatch.setattr(wanted_mod, 'wanted_company_http', lambda cid, **kw: fake_info)

    assert cli.main(['company', 'fetch', '--platform', 'wanted', '--id', '12345', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['company_id'] == 12345
    assert payload['tags'] == ['AI', 'Remote']


def test_cli_company_fetch_saramin_preserves_opaque_identifier(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import saramin as saramin_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    fake_info = saramin_mod.SaraminCompanyInfo(
        name='테스트',
        industry='Software',
        company_type='IT',
        founded_date='2020-01-01',
        employee_count=10,
        avg_salary_manwon=4000,
        ceo='홍길동',
        homepage='https://example.com',
        address='서울',
    )
    seen: list[str] = []

    def _fake(company_id: str, **kw):
        seen.append(company_id)
        return fake_info

    monkeypatch.setattr(saramin_mod, 'saramin_company_http', _fake)

    opaque_id = 'Q29tcGFueS0xMjM='
    assert cli.main(['company', 'fetch', '--platform', 'saramin', '--id', opaque_id]) == 0
    assert seen == [opaque_id]


@pytest.mark.parametrize('company_id', ['0', '-1', 'not-a-number'])
def test_cli_company_fetch_wanted_rejects_non_positive_identifier_before_http_call(
    monkeypatch, capsys, company_id: str
) -> None:
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    called = False

    def _unexpected(company_id: int, **kw):
        nonlocal called
        called = True
        raise AssertionError(company_id)

    monkeypatch.setattr(wanted_mod, 'wanted_company_http', _unexpected)

    assert cli.main(['company', 'fetch', '--platform', 'wanted', '--id', company_id]) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'error: Wanted company id must be a positive integer\n'
    assert called is False


def test_cli_company_fetch_wanted_surfaces_stable_public_error(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    def _fail(company_id: int, **kw):
        raise RuntimeError('missing internal payload details')

    monkeypatch.setattr(wanted_mod, 'wanted_company_http', _fail)

    assert cli.main(['company', 'fetch', '--platform', 'wanted', '--id', '12345']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'error: failed to fetch Wanted company info\n'


def test_cli_company_fetch_wanted_dispatches_packaged_command(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    seen: list[int] = []
    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name='테스트',
        industry='Software',
        founded_year=None,
        location='서울',
        employee_count=None,
        avg_salary_manwon=None,
        hired_1y=None,
        left_1y=None,
        total_sales_eok=None,
        sales_year='',
        tags=(),
        description='',
        homepage='',
    )

    def _fake(company_id: int, **kw):
        seen.append(company_id)
        return fake_info

    monkeypatch.setattr(wanted_mod, 'wanted_company_http', _fake)

    assert cli.main(['company', 'fetch', '--platform', 'wanted', '--id', '12345', '--json']) == 0
    assert seen == [12345]
    assert json.loads(capsys.readouterr().out)['company_id'] == 12345


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
        'fallback_reason': None,
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


def test_cli_company_apply_uses_packaged_locked_writer(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    source = tmp_path / 'candidate.md'
    source.write_text(
        '# Acme\n\n'
        '## 기업 정보\n\n'
        '| 항목 | 내용 |\n|------|------|\n'
        '| 설립 | 2021년 |\n'
        '| 직원수 | 12명 |\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    assert cli.main(['company', 'apply', '--company-name', 'Acme', '--input', str(source), '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['command'] == 'company apply'
    assert payload['status'] == 'ready'
    assert payload['persisted'] is True
    assert payload['file_path'] == 'private/company_info/acme.md'


def test_cli_company_apply_returns_usage_error_for_digest_conflict(monkeypatch, capsys, tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source='explicit')
    bundle = cli.ServiceBundle(
        maintenance=FakeMaintenance(),
        pipeline=FakePipeline(),
        automation=FakeAutomation(),
    )
    company_dir = tmp_path / 'private' / 'company_info'
    company_dir.mkdir(parents=True)
    company_file = company_dir / 'acme.md'
    company_file.write_text(
        '# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 11명 |\n',
        encoding='utf-8',
    )
    source = tmp_path / 'candidate.md'
    source.write_text(
        '# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 12명 |\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)

    digest = cli.CompanyInfoService(workspace=workspace).inspect('Acme').digest
    assert isinstance(digest, str)
    company_file.write_text(
        '# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 13명 |\n',
        encoding='utf-8',
    )

    assert cli.main(['company', 'apply', '--company-name', 'Acme', '--input', str(source), '--expected-digest', digest]) == 2
    assert 'digest' in capsys.readouterr().err


def _prescreened_metadata(
    job_id: str,
    *,
    reason: str | None = None,
    verdict: ScreeningVerdict | None = None,
):
    from careerkit.jobs.adapters.storage.file_records import StoredJobMetadata

    return StoredJobMetadata(
        record=JobRecord(
            'wanted',
            job_id,
            'Acme',
            'Backend',
            screening_verdict=verdict,
            prescreen_reason=reason,
        ),
        has_screening=False,
    )


class _PrescreenedPipeline(FakePipeline):
    def __init__(self, set_aside=(), legacy=()) -> None:
        super().__init__()
        self.listing = (list(set_aside), list(legacy))
        self.reason_calls: list[str | None] = []

    def list_prescreened(self, *, reason: str | None = None) -> PrescreenedListing:
        self.reason_calls.append(reason)
        set_aside, legacy = self.listing
        if reason is not None:
            set_aside = [item for item in set_aside if item.record.prescreen_reason == reason]
            legacy = [item for item in legacy if item.record.prescreen_reason == reason]
        return PrescreenedListing(set_aside=list(set_aside), legacy=list(legacy))


class _PrescreenedRepository:
    """Fake record store whose screening document appears once screening runs."""

    def __init__(self) -> None:
        self.screened: set[str] = set()

    def get_metadata(self, key: JobKey):
        from careerkit.jobs.adapters.storage.file_records import StoredJobMetadata

        return StoredJobMetadata(
            record=JobRecord('wanted', key.job_id, 'Acme', 'Backend'),
            has_screening=key.job_id in self.screened,
        )

    def get(self, key: JobKey):
        return SimpleNamespace(
            record=JobRecord('wanted', key.job_id, 'Acme', 'Backend'),
            jd_markdown='# JD',
        )


def _prescreened_cli(monkeypatch, tmp_path: Path, pipeline, repository=None):
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
        pipeline=pipeline,
        automation=FakeAutomation(),
    )
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: bundle)
    monkeypatch.setattr(cli, 'JDRecordRepository', lambda path: repository or _PrescreenedRepository())
    monkeypatch.setattr(cli, 'CompanyInfoService', FakeCompanyInfo)
    monkeypatch.setattr(cli, 'load_candidate_context', lambda workspace: 'context')
    return bundle


def test_queue_prescreened_counts_only_records_that_got_a_document(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    # Deriving these from the selected target list makes them contradict
    # `rescreened` and `still_unscreened` in the same payload.
    pipeline = _PrescreenedPipeline(
        set_aside=[
            _prescreened_metadata('1', reason='title_exclude'),
            _prescreened_metadata('2', reason='title_exclude'),
        ],
        legacy=[_prescreened_metadata('3', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        job_id = kwargs['jd'].record.job_id
        if job_id == '2':
            return _screening(verdict='지원 보류', provider='ollama', published=False)
        repository.screened.add(job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(
        ['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '3', '--json']
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['rescreened'] == 2
    assert payload['still_unscreened'] == 1
    assert payload['set_aside_screened'] == 1
    assert payload['legacy_screened'] == 1


def test_queue_prescreened_screen_fails_when_classification_fails_after_publish(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    # The JSON reports the failure either way; exiting 0 lets a script read a
    # partially failed batch as clean.
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    def _classify(key, *, dry_run=False):
        raise RuntimeError('classification blew up after the document was published')

    monkeypatch.setattr(cli, 'run_screening', _run)
    monkeypatch.setattr(pipeline, 'classify_record', _classify)

    assert cli.main(['queue', 'prescreened', '--screen', '--limit', '1', '--json']) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload['failed_after_publish'] == 1
    assert payload['failed'] == 0
    assert payload['set_aside_screened'] == 0


def test_queue_prescreened_lists_set_aside_and_legacy_separately(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list']) == 0

    out = capsys.readouterr().out
    assert 'set aside' in out
    assert 'legacy' in out
    set_aside_at = out.index('set aside')
    legacy_at = out.index('legacy')
    assert set_aside_at < out.index('wanted:1') < legacy_at < out.index('wanted:2')
    assert 'title_exclude' in out


def test_queue_prescreened_screen_requires_limit(monkeypatch, capsys, tmp_path: Path) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[
            _prescreened_metadata('1', reason='title_exclude'),
            _prescreened_metadata('2', reason='title_exclude'),
        ],
        legacy=[_prescreened_metadata('3', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, 'run_screening', lambda **kwargs: calls.append(kwargs))

    assert cli.main(['queue', 'prescreened', '--screen']) == 1

    assert calls == []
    assert '2' in capsys.readouterr().err


def test_queue_prescreened_screen_requires_limit_counts_legacy_when_included(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, 'run_screening', lambda **kwargs: calls.append(kwargs))

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--json']) == 1

    assert calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload['backlog'] == 2
    assert 'error' in payload


def test_queue_prescreened_json_separates_verdict_bearing_records(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['set_aside_count'] == 1
    assert payload['legacy_count'] == 1
    assert payload['set_aside'] == [
        {
            'job_key': 'wanted:1',
            'company': 'Acme',
            'prescreen_reason': 'title_exclude',
        }
    ]
    assert payload['legacy'] == [
        {
            'job_key': 'wanted:2',
            'company': 'Acme',
            'screening_verdict': 'not_recommended',
        }
    ]


@pytest.mark.parametrize('limit', ['0', '-1'])
def test_queue_prescreened_rejects_a_non_positive_limit(
    monkeypatch, capsys, tmp_path: Path, limit: str
) -> None:
    _prescreened_cli(monkeypatch, tmp_path, _PrescreenedPipeline())

    with pytest.raises(SystemExit) as excinfo:
        cli.main(['queue', 'prescreened', '--list', '--limit', limit])

    assert excinfo.value.code == 2
    assert 'positive integer' in capsys.readouterr().err


def test_queue_prescreened_screen_skips_legacy_without_include_legacy(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    screened: list[str] = []

    def _run(**kwargs):
        job_id = kwargs['jd'].record.job_id
        screened.append(job_id)
        repository.screened.add(job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--limit', '5', '--json']) == 0

    assert screened == ['1']
    payload = json.loads(capsys.readouterr().out)
    assert payload['rescreened'] == 1
    assert payload['still_unscreened'] == 0


def test_queue_prescreened_screen_includes_legacy_when_asked(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    screened: list[str] = []

    def _run(**kwargs):
        job_id = kwargs['jd'].record.job_id
        screened.append(job_id)
        repository.screened.add(job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '5']) == 0

    assert screened == ['1', '2']


def test_queue_prescreened_screen_caps_the_batch_at_the_limit(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[
            _prescreened_metadata('1', reason='title_exclude'),
            _prescreened_metadata('2', reason='title_exclude'),
        ],
        legacy=[_prescreened_metadata('3', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    screened: list[str] = []

    def _run(**kwargs):
        job_id = kwargs['jd'].record.job_id
        screened.append(job_id)
        repository.screened.add(job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '2']) == 0

    assert screened == ['1', '2']


def test_queue_prescreened_screen_does_not_require_a_strong_provider(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    seen: list[bool] = []

    def _run(**kwargs):
        seen.append(kwargs['require_strong_provider'])
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--limit', '1']) == 0

    assert seen == [False]


def test_queue_prescreened_screen_reports_a_record_left_unscreened(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    monkeypatch.setattr(cli, 'run_screening', lambda **kwargs: _screening())

    assert cli.main(['queue', 'prescreened', '--screen', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['rescreened'] == 0
    assert payload['still_unscreened'] == 1


def test_queue_prescreened_passes_the_reason_filter_through(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[
            _prescreened_metadata('1', reason='title_exclude'),
            _prescreened_metadata('2', reason='backend_override'),
        ],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list', '--reason', 'backend_override', '--json']) == 0

    assert pipeline.reason_calls == ['backend_override']
    payload = json.loads(capsys.readouterr().out)
    assert [item['job_key'] for item in payload['set_aside']] == ['wanted:2']


def test_queue_prescreened_rejects_list_and_screen_together(monkeypatch, tmp_path: Path) -> None:
    _prescreened_cli(monkeypatch, tmp_path, _PrescreenedPipeline())

    with pytest.raises(SystemExit) as excinfo:
        cli.main(['queue', 'prescreened', '--list', '--screen'])

    assert excinfo.value.code == 2


def test_queue_prescreened_list_limit_reports_the_true_backlog(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata(str(index), reason='title_exclude') for index in range(1, 4)],
        legacy=[
            _prescreened_metadata('8', verdict=ScreeningVerdict.NOT_RECOMMENDED),
            _prescreened_metadata('9', verdict=ScreeningVerdict.NOT_RECOMMENDED),
        ],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list', '--limit', '1']) == 0

    out = capsys.readouterr().out
    assert '3 records set aside by pre-screen (showing 1):' in out
    assert '2 legacy verdicts without a screening document (showing 1):' in out
    assert 'wanted:2' not in out


def test_queue_prescreened_list_json_reports_the_true_backlog(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata(str(index), reason='title_exclude') for index in range(1, 4)],
        legacy=[
            _prescreened_metadata('8', verdict=ScreeningVerdict.NOT_RECOMMENDED),
            _prescreened_metadata('9', verdict=ScreeningVerdict.NOT_RECOMMENDED),
        ],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['set_aside_count'] == 3
    assert payload['legacy_count'] == 2
    assert payload['set_aside_shown'] == 1
    assert payload['legacy_shown'] == 1
    assert len(payload['set_aside']) == 1
    assert len(payload['legacy']) == 1


def test_queue_prescreened_list_omits_the_showing_suffix_when_nothing_is_hidden(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
    )
    _prescreened_cli(monkeypatch, tmp_path, pipeline)

    assert cli.main(['queue', 'prescreened', '--list', '--limit', '5']) == 0

    assert 'showing' not in capsys.readouterr().out


def test_queue_prescreened_screen_says_include_legacy_reached_nothing(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[
            _prescreened_metadata('1', reason='title_exclude'),
            _prescreened_metadata('2', reason='title_exclude'),
        ],
        legacy=[_prescreened_metadata('3', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)
    screened: list[str] = []

    def _run(**kwargs):
        job_id = kwargs['jd'].record.job_id
        screened.append(job_id)
        repository.screened.add(job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '2']) == 0

    assert screened == ['1', '2']
    out = capsys.readouterr().out
    assert '--include-legacy screened no legacy record' in out
    assert '1 legacy records waiting' in out


def test_queue_prescreened_screen_json_carries_the_legacy_notice(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['set_aside_screened'] == 1
    assert payload['legacy_screened'] == 0
    assert payload['notice'] is not None


def test_queue_prescreened_screen_is_silent_when_legacy_records_fit(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '5', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['legacy_screened'] == 1
    assert payload['notice'] is None


def test_queue_prescreened_screen_is_silent_when_no_legacy_record_exists(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    # Without this case, dropping the `legacy_total` guard from the notice's trigger
    # passes the whole suite while emitting "0 legacy records waiting" to a user whose
    # backlog has no legacy record at all.
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--include-legacy', '--limit', '1', '--json']) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload['legacy_screened'] == 0
    assert payload['notice'] is None


def test_queue_prescreened_screen_is_silent_without_include_legacy(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    pipeline = _PrescreenedPipeline(
        set_aside=[_prescreened_metadata('1', reason='title_exclude')],
        legacy=[_prescreened_metadata('2', verdict=ScreeningVerdict.NOT_RECOMMENDED)],
    )
    repository = _PrescreenedRepository()
    _prescreened_cli(monkeypatch, tmp_path, pipeline, repository)

    def _run(**kwargs):
        repository.screened.add(kwargs['jd'].record.job_id)
        return _screening(verdict='지원 보류', provider='ollama', published=True)

    monkeypatch.setattr(cli, 'run_screening', _run)

    assert cli.main(['queue', 'prescreened', '--screen', '--limit', '1', '--json']) == 0

    assert json.loads(capsys.readouterr().out)['notice'] is None

def test_cli_company_fetch_thevc_prints_json_with_lists(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import thevc as thevc_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    fake_info = thevc_mod.TheVCCompanyInfo(
        name='포지큐브',
        name_en='POSICUBE',
        founded_on='2017-05-23',
        ceo_name='오성조',
        ceo_is_founder=True,
        keywords=('AI기술', '인공지능'),
        products=('robi리셉션', 'AI에이전트'),
        last_round='Series B',
        last_funded_on='2021-11-09',
        total_funding_count=4,
        investor_count_total=9,
        funding_rounds=(
            thevc_mod.TheVCFundingRound(round_name='Series B', funded_on='2021-11-09', funding_type='시리즈 B'),
        ),
        slug='posicube',
    )
    monkeypatch.setattr(thevc_mod, 'thevc_company_http', lambda slug, **kw: fake_info)

    assert cli.main(['company', 'fetch', '--platform', 'thevc', '--id', 'posicube', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['name'] == '포지큐브'
    assert data['slug'] == 'posicube'
    assert data['last_funded_on'] == '2021-11-09'
    assert data['keywords'] == ['AI기술', '인공지능']
    assert data['products'] == ['robi리셉션', 'AI에이전트']
    assert data['funding_rounds'] == [
        {'round_name': 'Series B', 'funded_on': '2021-11-09', 'funding_type': '시리즈 B'}
    ]


def test_cli_company_fetch_thevc_error_returns_1(monkeypatch, capsys) -> None:
    from careerkit.jobs.adapters.platforms import thevc as thevc_mod

    workspace = WorkspacePaths(root=Path('/workspace'), source='explicit')
    monkeypatch.setattr(cli, 'resolve_workspace', lambda explicit=None: workspace)
    monkeypatch.setattr(cli, '_build_services', lambda resolved: cli.ServiceBundle(
        maintenance=FakeMaintenance(), pipeline=FakePipeline(), automation=FakeAutomation(),
    ))

    def _fail(slug: str, **kw):
        raise ValueError('company not found')

    monkeypatch.setattr(thevc_mod, 'thevc_company_http', _fail)

    assert cli.main(['company', 'fetch', '--platform', 'thevc', '--id', 'ghost']) == 1
    err = capsys.readouterr().err
    assert 'error' in err.lower()
    assert 'company not found' in err
