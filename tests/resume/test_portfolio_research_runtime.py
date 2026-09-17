from __future__ import annotations

from pathlib import Path
from hashlib import sha256

import pytest

from careerkit.resume import portfolio_research_runtime


def test_runtime_prints_aggregate_counts_only(tmp_path: Path, capsys: object) -> None:
    exit_status = portfolio_research_runtime.main(["--root", str(tmp_path), "discovery"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 2
    assert captured.out == "checks=1 errors=5 warnings=0\n"
    assert str(tmp_path) not in captured.out


def test_runtime_rejects_root_outside_source_workspace(tmp_path: Path, capsys: object) -> None:
    exit_status = portfolio_research_runtime.main(
        ["--root", str(tmp_path), "--source-root", str(tmp_path / "other"), "discovery"]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 2
    assert captured.out == "checks=1 errors=1 warnings=0\n"


def test_headless_digest_mismatch_exits_before_journal_creation(tmp_path: Path, capsys: object) -> None:
    planning_input = tmp_path / "planning-inputs.md"
    planning_input.write_text("approved\n", encoding="utf-8")
    root = tmp_path / "portfolio-research"

    exit_status = portfolio_research_runtime.main(
        [
            "--root",
            str(root),
            "--source-root",
            str(tmp_path),
            "--planning-input",
            str(planning_input),
            "--expected-digest",
            sha256(b"changed\n").hexdigest(),
            "discovery",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 2
    assert captured.out == "checks=1 errors=1 warnings=0\n"
    assert not (root / "transactions").exists()


def test_cleanup_headless_runtime_reports_preparation_only(tmp_path: Path, capsys: object) -> None:
    exit_status = portfolio_research_runtime.main(
        ["--root", str(tmp_path / "portfolio-research"), "--cleanup-headless"]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 2
    assert captured.out == "cleanup=prepared replacements=0 errors=1\n"
    assert not (tmp_path / "portfolio-research" / "transactions").exists()


def test_cleanup_headless_rejects_before_loading_without_approved_digest(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = False

    def unexpected_load(_path: Path) -> tuple[list[object], list[object]]:
        nonlocal loaded
        loaded = True
        return [], []

    monkeypatch.setattr(portfolio_research_runtime, "load_catalog", unexpected_load)

    exit_status = portfolio_research_runtime.main(
        ["--root", str(tmp_path / "portfolio-research"), "--cleanup-headless"]
    )
    capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 2
    assert loaded is False


@pytest.mark.parametrize(
    "checks",
    [
        ("lane",),
        ("lane", "lane-5"),
        ("lane", "lane-1", "lane-2"),
        ("lane", "lane-1", "mystery"),
    ],
)
def test_runtime_rejects_invalid_lane_check_forms(
    tmp_path: Path,
    checks: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as raised:
        portfolio_research_runtime.main(["--root", str(tmp_path), *checks])

    assert raised.value.code == 2


def test_runtime_passes_lane_selector_and_unordered_flags_as_one_request(
    tmp_path: Path,
    capsys: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, ...]] = []

    def validate(_root: Path, checks: tuple[str, ...]) -> list[object]:
        received.append(tuple(checks))
        return []

    monkeypatch.setattr(portfolio_research_runtime, "validate_research", validate)

    exit_status = portfolio_research_runtime.main(
        [
            "--root",
            str(tmp_path),
            "privacy",
            "lane",
            "lane-2",
            "dispositions",
            "evidence-shape",
        ]
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]

    assert exit_status == 0
    assert received == [
        ("privacy", "lane", "lane-2", "dispositions", "evidence-shape")
    ]
    assert captured.out == "checks=4 errors=0 warnings=0\n"
