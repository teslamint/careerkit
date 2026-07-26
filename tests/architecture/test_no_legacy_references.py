from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess

from .active_surfaces import ROOT, active_surface_paths
from .legacy_paths import REMOVED_PATHS


LEGACY_COMMANDS = (
    re.compile(r"(?:^|[;&|]\s*)\./build\.sh(?:\s|$)"),
    re.compile(r"python(?:3)?\s+-m\s+templates\."),
    re.compile(r"scripts/screen-jds\.sh"),
)
REMOVED_ROLE_KEYS = (
    "job_group_id",
    "job_ids",
    "job_category_names",
    "position_types",
)
TEXT_SUFFIXES = {".md", ".json", ".toml", ".yaml", ".yml", ".sh", ".txt"}
LEGACY_REFERENCE_ALLOWLIST = {
    Path("docs/architecture/entrypoint-inventory.md"),
    Path("docs/architecture/legacy-behavior-parity-audit.md"),
}


def _python_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_legacy_reference_allowlist_is_limited_to_disposition_evidence() -> None:
    assert LEGACY_REFERENCE_ALLOWLIST == {
        Path("docs/architecture/entrypoint-inventory.md"),
        Path("docs/architecture/legacy-behavior-parity-audit.md"),
    }


def test_legacy_paths_are_absent_after_cutover() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    repository_files = result.stdout.splitlines()
    offenders = [
        path
        for path in repository_files
        if any(path == str(removed) or path.startswith(f"{removed}/") for removed in REMOVED_PATHS)
    ]
    assert offenders == []


def test_active_python_surfaces_do_not_import_legacy_templates() -> None:
    offenders: list[str] = []
    for path in active_surface_paths():
        if path.suffix != ".py":
            continue
        for imported in _python_imports(path):
            if imported == "templates" or imported.startswith("templates."):
                offenders.append(f"{path.relative_to(ROOT)}:{imported}")
    assert offenders == []


def test_active_callers_do_not_invoke_removed_commands() -> None:
    offenders: list[str] = []
    for path in active_surface_paths():
        if path.suffix not in TEXT_SUFFIXES or path.name == "test_no_legacy_references.py":
            continue
        if path.relative_to(ROOT) in LEGACY_REFERENCE_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in LEGACY_COMMANDS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}:{pattern.pattern}")
    assert offenders == []


def test_active_configuration_and_guidance_do_not_restore_raw_role_keys() -> None:
    offenders: list[str] = []
    for path in active_surface_paths():
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"src", "tests"}:
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for key in REMOVED_ROLE_KEYS:
            if re.search(rf"(?m)^\s*{re.escape(key)}\s*:", text):
                offenders.append(f"{relative}:{key}")
    assert offenders == []
