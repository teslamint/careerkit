from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from tests.architecture.legacy_paths import REMOVED_FILES, REMOVED_ROOTS


ROOT = Path(__file__).resolve().parents[2]


def _run(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{' '.join(argv)} failed with {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def _export_index(candidate: Path) -> None:
    candidate.mkdir(parents=True, exist_ok=True)
    prefix = f"{candidate.resolve()}/"
    _run(["git", "checkout-index", "--all", f"--prefix={prefix}"], cwd=ROOT)
    for relative in REMOVED_ROOTS:
        shutil.rmtree(candidate / relative, ignore_errors=True)
    for relative in REMOVED_FILES:
        (candidate / relative).unlink(missing_ok=True)


def _initialize_candidate_index(candidate: Path) -> None:
    _run(["git", "init", "--quiet"], cwd=candidate)
    _run(["git", "add", "--all"], cwd=candidate)


def _candidate_env() -> dict[str, str]:
    """Return the environment for toolchain commands run inside the candidate.

    The candidate runs ``uv``, which picks its project environment from
    ``UV_PROJECT_ENVIRONMENT`` and ``VIRTUAL_ENV``. Inheriting either makes the
    child re-sync the *parent* suite's environment to the exported candidate
    project. The candidate directory is deleted afterwards, so the parent
    environment is left pointing at a missing tree and every later test that
    spawns a subprocess fails with ``ModuleNotFoundError: careerkit``. Drop both
    so the child resolves its own ``.venv`` inside the candidate directory.
    """
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    cache_dir = Path(env.get("UV_CACHE_DIR", "/tmp/resume-uv-cache")).resolve()
    env.update(
        {
            "CAREERKIT_CLEAN_CANDIDATE": "1",
            "UV_CACHE_DIR": str(cache_dir),
        }
    )
    return env



@pytest.mark.skipif(
    os.environ.get("CAREERKIT_CLEAN_CANDIDATE") == "1",
    reason="avoid recursive clean-candidate export inside the candidate suite",
)
def test_index_exports_a_legacy_free_candidate_that_passes_package_proof() -> None:
    temp_root = ROOT / ".tmp"
    temp_root.mkdir(exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix="clean-candidate-", dir=temp_root))
    try:
        _export_index(candidate)

        for relative in REMOVED_ROOTS + REMOVED_FILES:
            assert not (candidate / relative).exists(), relative
        assert not list(candidate.rglob("*.sh"))

        _initialize_candidate_index(candidate)
        env = _candidate_env()
        commands = (
            ["uv", "build", "--out-dir", str(candidate / "dist")],
            ["uv", "run", "pytest", "tests", "-q"],
            ["uv", "run", "ruff", "check", "src", "tests"],
            ["uv", "run", "pyright", "src/careerkit", "tests"],
            ["uv", "run", "lint-imports"],
            [
                "uv",
                "run",
                "vulture",
                "src",
                "tests/static/vulture_whitelist.py",
                "--min-confidence",
                "60",
            ],
        )
        for command in commands:
            _run(command, cwd=candidate, env=env)
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def test_candidate_env_does_not_inherit_the_parent_project_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/tmp/parent-suite-venv")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/parent-suite-venv")

    env = _candidate_env()

    assert "UV_PROJECT_ENVIRONMENT" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["CAREERKIT_CLEAN_CANDIDATE"] == "1"
