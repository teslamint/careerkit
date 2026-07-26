from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


FIXTURE_WORKSPACE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "workspace" / "basic"
)


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resume_help_does_not_require_workspace(tmp_path: Path) -> None:
    result = _run_cli("careerkit.resume.cli", "--help", cwd=tmp_path)

    assert result.returncode == 0
    assert "career-resume" in result.stdout
    assert "workspace not found" not in result.stderr


def test_jobs_help_does_not_require_workspace(tmp_path: Path) -> None:
    result = _run_cli("careerkit.jobs.cli", "--help", cwd=tmp_path)

    assert result.returncode == 0
    assert "career-jobs" in result.stdout
    assert "workspace not found" not in result.stderr


def test_data_command_resolves_workspace_before_placeholder_message(tmp_path: Path) -> None:
    missing = tmp_path / "missing-workspace"
    result = _run_cli(
        "careerkit.jobs.cli",
        "--workspace",
        str(missing),
        "run",
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "workspace path does not exist" in result.stderr
    assert "placeholder" not in result.stdout


def test_jobs_json_config_check_uses_explicit_workspace_without_disclosure() -> None:
    result = _run_cli(
        "careerkit.jobs.cli",
        "--workspace",
        str(FIXTURE_WORKSPACE),
        "config",
        "check",
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "config check"
    assert payload["status"] == "ok"
    assert payload["normalized_role"] == "backend"
    assert payload["workspace_source"] == "explicit"
    assert payload["workspace_root"] == "."


@pytest.mark.parametrize(
    ("distribution_name", "expected_target"),
    [
        ("careerkit", "careerkit.resume.cli:main"),
        ("careerkit", "careerkit.jobs.cli:main"),
    ],
)
def test_console_script_entry_points_present(
    distribution_name: str, expected_target: str
) -> None:
    import importlib.metadata as metadata

    distribution = metadata.distribution(distribution_name)
    entry_points = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }

    if expected_target.startswith("careerkit.resume"):
        assert entry_points["career-resume"] == expected_target
    else:
        assert entry_points["career-jobs"] == expected_target
