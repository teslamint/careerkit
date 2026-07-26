from __future__ import annotations

from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ROOT_FILES = (
    "ARCHITECTURE.md",
    "README.md",
    "CONTRIBUTING.md",
    "CLAUDE.md",
    "USER_DATA.md",
    "AGENTS.md",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
)
ACTIVE_TREES = (
    "docs/getting-started.md",
    "docs/customization.md",
    "docs/ai-workflow.md",
    "docs/config-state-contract.md",
    "docs/examples/codex-hooks.json",
    ".claude/settings.json",
    ".claude/skills",
    ".codex/hooks.json",
    ".codex/skills",
    ".github/workflows/ci.yml",
)
LEGACY_PATTERNS = (
    re.compile(r"\./build\.sh"),
    re.compile(r"python(?:3)?\s+-m\s+templates\."),
    re.compile(r"templates\.jd\.screening_lint"),
)


def _active_files() -> list[Path]:
    paths = [ROOT / name for name in ACTIVE_ROOT_FILES]
    for name in ACTIVE_TREES:
        path = ROOT / name
        paths.extend(sorted(path.rglob("*.md")) if path.is_dir() else [path])
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.json")))
    return sorted(set(paths))


def test_active_callers_do_not_reference_legacy_entrypoints() -> None:
    offenders: list[str] = []
    for path in _active_files():
        text = path.read_text(encoding="utf-8")
        for pattern in LEGACY_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}:{pattern.pattern}")
    assert offenders == []


def test_claude_and_codex_skills_share_command_storage_and_privacy_contracts() -> None:
    claude = {path.parent.name: path for path in (ROOT / ".claude/skills").glob("*/SKILL.md")}
    codex = {path.parent.name: path for path in (ROOT / ".codex/skills").glob("*/SKILL.md")}
    assert codex.keys() == claude.keys()
    for name in sorted(claude):
        claude_text = claude[name].read_text(encoding="utf-8")
        codex_text = codex[name].read_text(encoding="utf-8")
        assert codex_text == claude_text, name


def test_codex_skills_are_tracked_while_runtime_state_remains_ignored() -> None:
    candidates = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", ".codex/skills", ".codex/hooks.json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    ignored = subprocess.run(
        ["git", "check-ignore", "-v", ".codex/runtime-state.json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ".codex/hooks.json" in candidates
    assert ".codex/skills/jd-screening/SKILL.md" in candidates
    assert subprocess.run(
        ["git", "check-ignore", "-q", ".codex/skills/jd-screening/SKILL.md"],
        cwd=ROOT,
        check=False,
    ).returncode == 1
    assert ignored.returncode == 0


def test_docker_context_is_deny_by_default_and_allows_only_package_inputs() -> None:
    lines = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[0] == "**"
    assert "!src/**" in lines
    assert "!pyproject.toml" in lines
    assert not any("private" in line or "worktree" in line for line in lines[1:])
