from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "docs" / "architecture" / "test-inventory.md"
FINAL_FAMILIES = (
    "tests/architecture",
    "tests/contract",
    "tests/jobs/application",
    "tests/jobs/console",
    "tests/jobs/platforms",
    "tests/jobs/storage",
    "tests/resume",
)


def test_top_level_tests_are_classified_into_current_stage_buckets() -> None:
    root = ROOT / "tests"
    allowed = {"architecture", "contract", "docs", "ext", "jobs", "resume"}
    observed = {
        path.relative_to(root).parts[0]
        for path in root.rglob("test_*.py")
        if "__pycache__" not in path.parts
    }
    assert observed <= allowed


def test_every_baseline_test_has_reviewed_final_disposition() -> None:
    text = INVENTORY.read_text(encoding="utf-8")
    modules = re.findall(r"(?m)^- module: ([^ ]+) ", text)

    assert len(modules) == 82
    assert len(modules) == len(set(modules))
    assert all(module.startswith("templates/tests/") for module in modules)
    assert "Final disposition: all 82 legacy modules were reviewed and deleted" in text
    result = subprocess.run(
        ["git", "ls-files", "--cached", "templates/tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == []


def test_final_replacement_families_collect_behavior_tests() -> None:
    for relative in FINAL_FAMILIES:
        family = ROOT / relative
        assert family.is_dir(), relative
        assert list(family.rglob("test_*.py")), relative
