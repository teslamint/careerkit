from __future__ import annotations

from pathlib import Path


REQUIRED_CATEGORIES = {
    "Personal scripts",
    "Shell aliases and habitual commands",
    "Schedulers (launchd/cron/automation)",
    "Editor tasks / IDE run configs",
    "Device / push automation",
}


def test_external_caller_checklist_is_sanitized_and_complete() -> None:
    path = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "external-caller-checklist.md"
    text = path.read_text(encoding="utf-8")

    for category in REQUIRED_CATEGORIES:
        assert category in text
    assert "| Category | Status | Disposition | Notes |" in text
    assert "/Users/" not in text
    assert "~/." not in text
    assert "launchctl" not in text
    assert "$ " not in text
    assert "private/" not in text
