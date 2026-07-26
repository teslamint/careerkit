from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from careerkit.workspace import (
    MARKER_FILE_NAME,
    MARKER_VERSION,
    WorkspaceResolutionError,
    resolve_workspace,
)


def _write_workspace_marker(root: Path, *, version: str = MARKER_VERSION) -> Path:
    marker = root / MARKER_FILE_NAME
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{version}\n", encoding="utf-8")
    return marker


def _env(**values: str) -> Mapping[str, str]:
    return values


def test_explicit_workspace_override_beats_environment_and_discovery(tmp_path: Path) -> None:
    explicit_root = tmp_path / "explicit"
    env_root = tmp_path / "env"
    discovered_root = tmp_path / "discovered"
    _write_workspace_marker(explicit_root)
    _write_workspace_marker(env_root)
    nested = discovered_root / "nested" / "cwd"
    _write_workspace_marker(discovered_root)
    nested.mkdir(parents=True)

    resolved = resolve_workspace(
        explicit=explicit_root,
        cwd=nested,
        env=_env(CAREER_WORKSPACE=str(env_root)),
    )

    assert resolved.root == explicit_root.resolve()
    assert resolved.source == "explicit"


def test_environment_override_beats_discovery(tmp_path: Path) -> None:
    env_root = tmp_path / "env"
    discovered_root = tmp_path / "discovered"
    _write_workspace_marker(env_root)
    nested = discovered_root / "nested" / "cwd"
    _write_workspace_marker(discovered_root)
    nested.mkdir(parents=True)

    resolved = resolve_workspace(cwd=nested, env=_env(CAREER_WORKSPACE=str(env_root)))

    assert resolved.root == env_root.resolve()
    assert resolved.source == "environment"


def test_root_and_nested_discovery_resolve_the_same_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "a" / "b"
    _write_workspace_marker(root)
    nested.mkdir(parents=True)

    from_root = resolve_workspace(cwd=root)
    from_nested = resolve_workspace(cwd=nested)

    assert from_root.root == root.resolve()
    assert from_nested.root == root.resolve()
    assert from_root.source == "discovery"
    assert from_nested.source == "discovery"


def test_workspace_owns_canonical_jobs_paths(tmp_path: Path) -> None:
    _write_workspace_marker(tmp_path)

    workspace = resolve_workspace(explicit=tmp_path)

    assert workspace.jobs_dir == tmp_path / "private" / "jd"
    assert workspace.jobs_config_dir == workspace.jobs_dir / "config"
    assert workspace.jobs_records_dir == workspace.jobs_dir / "records"
    assert workspace.jobs_runtime_dir == workspace.jobs_dir / "runtime"
    assert workspace.jobs_derived_dir == workspace.jobs_dir / "derived"


@pytest.mark.parametrize(
    ("label", "prepare", "expected_fragment"),
    [
        (
            "missing-marker",
            lambda path: path.mkdir(parents=True, exist_ok=True),
            "missing .career-workspace marker",
        ),
        (
            "symlinked-marker",
            lambda path: _prepare_symlinked_marker(path),
            "must be a regular file",
        ),
        (
            "invalid-version",
            lambda path: _write_workspace_marker(path, version="2"),
            "unsupported marker version",
        ),
        (
            "escaped-path",
            lambda path: _prepare_escaped_path(path),
            "must point at the workspace root",
        ),
    ],
)
def test_explicit_workspace_failures_are_actionable(
    tmp_path: Path,
    label: str,
    prepare,
    expected_fragment: str,
) -> None:
    candidate = tmp_path / label
    prepare(candidate)

    with pytest.raises(WorkspaceResolutionError, match=expected_fragment):
        resolve_workspace(explicit=candidate, cwd=tmp_path)


def test_nested_markers_fail_discovery_actionably(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "nested"
    cwd = inner / "cwd"
    _write_workspace_marker(outer)
    _write_workspace_marker(inner)
    cwd.mkdir(parents=True)

    with pytest.raises(WorkspaceResolutionError, match="multiple workspace markers"):
        resolve_workspace(cwd=cwd)


def test_missing_workspace_reports_actionable_failure(tmp_path: Path) -> None:
    cwd = tmp_path / "outside"
    cwd.mkdir()

    with pytest.raises(WorkspaceResolutionError, match="Set --workspace or CAREER_WORKSPACE"):
        resolve_workspace(cwd=cwd)



def _prepare_symlinked_marker(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    target = root / "marker-target"
    target.write_text(f"{MARKER_VERSION}\n", encoding="utf-8")
    (root / MARKER_FILE_NAME).symlink_to(target)



def _prepare_escaped_path(root: Path) -> None:
    workspace_root = root.parent / f"{root.name}-real-root"
    _write_workspace_marker(workspace_root)
    (workspace_root / "nested").mkdir(parents=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "placeholder").write_text("not-a-workspace\n", encoding="utf-8")
