from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "docs" / "architecture" / "active-surfaces.txt"

HISTORICAL_ROOTS = (
    ".claude/plans/",
    ".entire/",
    ".entirecontext/",
    "docs/plans/",
    "docs/research/",
    "docs/superpowers/",
)

HISTORICAL_FILES = {
    "CHANGELOG.md",
    "LESSONS.md",
    "LICENSE",
}

ACTIVE_PREFIXES = (
    ".claude/skills/",
    ".codex/skills/",
    ".github/workflows/",
    "docker/",
    "docs/architecture/",
    "example/",
    "src/",
    "tests/",
)

ACTIVE_FILES = {
    ".career-workspace",
    ".claude/settings.json",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".dockerignore",
    ".gitignore",
    ".importlinter",
    ".python-version",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "Makefile",
    "README.md",
    "USER_DATA.md",
    "docker-compose.yml",
    "docs/ai-workflow.md",
    "docs/config-state-contract.md",
    "docs/customization.md",
    "docs/examples/codex-hooks.json",
    "docs/getting-started.md",
    "eslint.config.js",
    "mise.toml",
    "package-lock.json",
    "package.json",
    "pyproject.toml",
    "search_config.example.yaml",
    "verify_content_config.example.json",
}


def _tracked_and_untracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def is_historical_surface(path: str) -> bool:
    return path in HISTORICAL_FILES or any(path.startswith(prefix) for prefix in HISTORICAL_ROOTS)


def is_active_surface(path: str) -> bool:
    if is_historical_surface(path):
        return False
    return path in ACTIVE_FILES or any(path.startswith(prefix) for prefix in ACTIVE_PREFIXES)


def derive_active_surfaces() -> list[str]:
    return sorted(path for path in _tracked_and_untracked_files() if is_active_surface(path))


def load_active_surface_manifest() -> list[str]:
    return [
        line.strip()
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def active_surface_paths() -> list[Path]:
    return [ROOT / relative for relative in derive_active_surfaces()]
