from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_architecture_doc_covers_authoritative_boundaries_and_change_recipes() -> None:
    text = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    required = (
        "# Repository Architecture",
        "## Product boundaries",
        "## Data authority",
        "## Core flows",
        "## Public command map and exit codes",
        "### Add or change a JD search condition",
        "src/careerkit/jobs/application/config.py",
        "### Add a platform",
        "src/careerkit/jobs/adapters/platforms/",
        "### Add a resume output format",
        "src/careerkit/resume/application/build.py",
        "### Add or change a status rule",
        "src/careerkit/jobs/domain/model.py",
        "## Cutover rules",
    )
    for item in required:
        assert item in text


def test_architecture_doc_defines_only_two_public_executables() -> None:
    text = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Only two installed executables are public" in text
    assert "career-resume" in text
    assert "career-jobs" in text
