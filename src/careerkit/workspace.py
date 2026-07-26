from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Mapping

MARKER_FILE_NAME = ".career-workspace"
MARKER_VERSION = "1"
ENVIRONMENT_VARIABLE = "CAREER_WORKSPACE"


class WorkspaceResolutionError(RuntimeError):
    """Raised when a workspace cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path
    source: str

    @property
    def marker_path(self) -> Path:
        return self.root / MARKER_FILE_NAME

    @property
    def private_dir(self) -> Path:
        return self.root / "private"

    @property
    def cache_dir(self) -> Path:
        return self.private_dir / ".cache"

    @property
    def jobs_dir(self) -> Path:
        return self.private_dir / "jd"

    @property
    def jobs_config_dir(self) -> Path:
        return self.jobs_dir / "config"

    @property
    def jobs_records_dir(self) -> Path:
        return self.jobs_dir / "records"

    @property
    def jobs_runtime_dir(self) -> Path:
        return self.jobs_dir / "runtime"

    @property
    def jobs_derived_dir(self) -> Path:
        return self.jobs_dir / "derived"


def resolve_workspace(
    *,
    explicit: str | Path | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> WorkspacePaths:
    """Resolve a workspace root using explicit, env, then upward discovery."""

    real_cwd = _resolve_cwd(cwd)

    if explicit is not None:
        return _resolve_explicit_root(explicit, cwd=real_cwd, source="explicit")

    environment = os.environ if env is None else env
    env_value = environment.get(ENVIRONMENT_VARIABLE)
    if env_value:
        return _resolve_explicit_root(env_value, cwd=real_cwd, source="environment")

    return _discover_workspace(cwd=real_cwd)


def _resolve_cwd(cwd: str | Path | None) -> Path:
    base = Path.cwd() if cwd is None else Path(cwd)
    try:
        return base.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceResolutionError(f"current directory does not exist: {base}") from exc


def _resolve_explicit_root(candidate: str | Path, *, cwd: Path, source: str) -> WorkspacePaths:
    raw_path = Path(candidate).expanduser()
    candidate_path = raw_path if raw_path.is_absolute() else cwd / raw_path

    try:
        resolved_root = candidate_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceResolutionError(
            f"{source} workspace path does not exist: {candidate_path}"
        ) from exc

    if not resolved_root.is_dir():
        raise WorkspaceResolutionError(
            f"{source} workspace path must point to a directory: {resolved_root}"
        )

    marker_path = resolved_root / MARKER_FILE_NAME
    _validate_marker(marker_path, source=source, require_exact_root=True)
    return WorkspacePaths(root=resolved_root, source=source)


def _discover_workspace(*, cwd: Path) -> WorkspacePaths:
    marker_paths: list[Path] = []
    for directory in (cwd, *cwd.parents):
        marker_path = directory / MARKER_FILE_NAME
        if marker_path.exists() or marker_path.is_symlink():
            marker_paths.append(marker_path)

    if not marker_paths:
        raise WorkspaceResolutionError(
            "workspace not found from "
            f"{cwd}. Set --workspace or {ENVIRONMENT_VARIABLE}, or run inside a workspace "
            f"marked by {MARKER_FILE_NAME}."
        )

    if len(marker_paths) > 1:
        roots = ", ".join(str(path.parent) for path in marker_paths)
        raise WorkspaceResolutionError(
            "multiple workspace markers found during upward discovery: "
            f"{roots}. Remove nested {MARKER_FILE_NAME} files or pass --workspace explicitly."
        )

    marker_path = marker_paths[0]
    _validate_marker(marker_path, source="discovery", require_exact_root=False)
    return WorkspacePaths(root=marker_path.parent.resolve(strict=True), source="discovery")


def _validate_marker(marker_path: Path, *, source: str, require_exact_root: bool) -> None:
    if not marker_path.exists() and not marker_path.is_symlink():
        if require_exact_root:
            raise WorkspaceResolutionError(
                f"{source} workspace path must point at the workspace root; "
                f"missing {MARKER_FILE_NAME} marker in {marker_path.parent}."
            )
        raise WorkspaceResolutionError(
            f"discovered workspace marker missing at expected path: {marker_path}"
        )

    if marker_path.is_symlink() or not marker_path.is_file():
        raise WorkspaceResolutionError(
            f"{source} workspace marker {marker_path} must be a regular file, not a symlink or special file."
        )

    version = marker_path.read_text(encoding="utf-8").strip()
    if version != MARKER_VERSION:
        raise WorkspaceResolutionError(
            f"{source} workspace marker at {marker_path} has unsupported marker version {version!r}; "
            f"expected {MARKER_VERSION!r}."
        )
