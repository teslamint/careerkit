"""CLI integration tests for link subcommands."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from careerkit.jobs import cli
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.adapters.storage.link_store import LinkStore
from careerkit.jobs.application.linking import LinkService
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
)
from careerkit.workspace import WorkspacePaths


def _make_record(platform: str, job_id: str) -> JobRecord:
    return JobRecord(
        platform=platform,
        job_id=job_id,
        company="TestCo",
        position="Backend",
    )


@pytest.fixture
def env(tmp_path: Path):
    workspace = WorkspacePaths(root=tmp_path, source="test")
    records_dir = tmp_path / "private" / "jd" / "records"
    links_dir = tmp_path / "private" / "jd" / "links"

    repo = JDRecordRepository(records_dir)
    repo.create(_make_record("saramin", "111"), jd_markdown="# JD 1")
    repo.create(_make_record("wanted", "222"), jd_markdown="# JD 2")

    store = LinkStore(links_dir)
    link_service = LinkService(link_store=store, record_repo=repo)

    class FakeMaintenance:
        derived_dir = tmp_path / "derived"
        repository = repo
        runtime_dir = tmp_path / "runtime"
        def relative_path(self, path: Path) -> str:
            return str(path)

    class FakePipeline:
        repository = repo
        def show_record(self, key: JobKey) -> Any:
            from careerkit.jobs.adapters.storage.file_records import StoredJobMetadata
            stored = repo.get(key)
            return StoredJobMetadata(record=stored.record, has_screening=stored.screening_markdown is not None)
        def set_record_status(self, key, **kwargs):
            return repo.update_status(key, **kwargs)

    class FakeAutomation:
        pass

    services = cli.ServiceBundle(
        maintenance=cast(cli.MaintenanceOps, FakeMaintenance()),
        pipeline=cast(cli.PipelineOps, FakePipeline()),
        automation=cast(cli.AutomationOps, FakeAutomation()),
        link_service=link_service,
    )
    return workspace, services, repo


def _run(handler, args_dict, env):
    workspace, services, _ = env
    args = SimpleNamespace(**args_dict)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        rc = handler(args, workspace, services)
        stdout = sys.stdout.getvalue()
        stderr = sys.stderr.getvalue()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    return rc, stdout, stderr


def test_link_add(env):
    rc, stdout, _ = _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    assert rc == 0
    assert "created group" in stdout


def test_link_show(env):
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    rc, stdout, _ = _run(
        cli._handle_link_show,
        {"key": "saramin:111", "json": False},
        env,
    )
    assert rc == 0
    assert "saramin:111" in stdout
    assert "wanted:222" in stdout


def test_link_sync(env):
    _, _, repo = env
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    repo.update_status(JobKey("saramin", "111"), application_status=ApplicationStatus.APPLIED)

    rc, stdout, _ = _run(
        cli._handle_link_sync,
        {"key": "saramin:111", "dry_run": False, "json": False},
        env,
    )
    assert rc == 0
    assert "wanted:222" in stdout

    updated = repo.get(JobKey("wanted", "222"))
    assert updated.record.application_status == ApplicationStatus.APPLIED


def test_link_list(env):
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    rc, stdout, _ = _run(
        cli._handle_link_list,
        {"inconsistent": False, "json": False},
        env,
    )
    assert rc == 0
    assert "2 members" in stdout


def test_link_remove(env):
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    rc, stdout, _ = _run(
        cli._handle_link_remove,
        {"key": "saramin:111", "json": False},
        env,
    )
    assert rc == 0
    assert "group deleted" in stdout

    _, stdout2, _ = _run(
        cli._handle_link_show,
        {"key": "saramin:111", "json": False},
        env,
    )
    assert "소속된 링크 그룹 없음" in stdout2


def test_set_status_link_notice(env):
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    rc, _, stderr = _run(
        cli._handle_record_set_status,
        {
            "job_key": "saramin:111",
            "application_status": "applied",
            "posting_status": None,
            "application_status_updated_at": None,
            "application_note": None,
            "json": False,
        },
        env,
    )
    assert rc == 0
    assert "링크 그룹" in stderr
    assert "link sync" in stderr


def test_link_add_json(env):
    rc, stdout, _ = _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": "same", "json": True},
        env,
    )
    assert rc == 0
    payload = json.loads(stdout)
    assert payload["created"] is True
    assert payload["group_id"] is not None


def test_link_show_not_found(env):
    rc, stdout, _ = _run(
        cli._handle_link_show,
        {"key": "x:99", "json": False},
        env,
    )
    assert rc == 0
    assert "소속된 링크 그룹 없음" in stdout


def test_link_add_single_key_error(env):
    rc, _, stderr = _run(
        cli._handle_link_add,
        {"keys": ["saramin:111"], "note": None, "json": False},
        env,
    )
    assert rc == 2
    assert "at least 2" in stderr


def test_link_sync_dry_run(env):
    _, _, repo = env
    _run(
        cli._handle_link_add,
        {"keys": ["saramin:111", "wanted:222"], "note": None, "json": False},
        env,
    )
    repo.update_status(JobKey("saramin", "111"), application_status=ApplicationStatus.APPLIED)

    rc, stdout, _ = _run(
        cli._handle_link_sync,
        {"key": "saramin:111", "dry_run": True, "json": False},
        env,
    )
    assert rc == 0
    assert "DRY-RUN" in stdout

    still_pending = repo.get(JobKey("wanted", "222"))
    assert still_pending.record.application_status == ApplicationStatus.PENDING
