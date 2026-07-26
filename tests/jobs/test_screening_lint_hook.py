from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _post_tool_command(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["hooks"]["PostToolUse"][0]["hooks"][0]["command"]


def test_claude_and_codex_hooks_use_native_screening_lint_hook_command() -> None:
    claude = _post_tool_command(ROOT / ".claude" / "settings.json")
    codex = _post_tool_command(ROOT / ".codex" / "hooks.json")
    example = _post_tool_command(ROOT / "docs" / "examples" / "codex-hooks.json")

    assert "career-jobs screening lint --hook" in claude
    assert "career-jobs screening lint --hook" in codex
    assert "career-jobs screening lint --hook" in example
    assert "screening validate" not in claude + codex + example


def test_hook_commands_preserve_native_stdin_payload() -> None:
    for path in [
        ROOT / ".claude" / "settings.json",
        ROOT / ".codex" / "hooks.json",
        ROOT / "docs" / "examples" / "codex-hooks.json",
    ]:
        command = _post_tool_command(path)
        assert "career-jobs screening lint --hook" in command
        assert "cat " not in command
        assert "printf '%s" not in command
