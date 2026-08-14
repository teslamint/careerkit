from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs import probe_prescreen_confirmation as probe
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.domain.model import JobKey, JobRecord


class _Workspace:
    def __init__(self, config_dir: Path) -> None:
        self.jobs_config_dir = config_dir


def _write_config(tmp_path: Path, body: str) -> _Workspace:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "search_config.yaml").write_text(body, encoding="utf-8")
    return _Workspace(config_dir)


def test_load_quick_filters_returns_the_inner_mapping(tmp_path: Path) -> None:
    workspace = _write_config(
        tmp_path,
        "search:\n  role: backend\nquick_filters:\n  title_exclude:\n    - Synthetic Excluded\n",
    )

    assert probe.load_quick_filters(workspace) == {"title_exclude": ["Synthetic Excluded"]}


def test_load_quick_filters_refuses_an_empty_exclusion_list(tmp_path: Path) -> None:
    # An empty exclusion list makes every record confirm, so the probe would report
    # success while the filter under test never ran. It must refuse, not fall back.
    workspace = _write_config(tmp_path, "quick_filters:\n  title_exclude: []\n")

    with pytest.raises(SystemExit, match="non-empty list"):
        probe.load_quick_filters(workspace)


def test_load_quick_filters_refuses_a_missing_section(tmp_path: Path) -> None:
    workspace = _write_config(tmp_path, "search:\n  role: backend\n")

    with pytest.raises(SystemExit, match="quick_filters missing"):
        probe.load_quick_filters(workspace)


def test_load_quick_filters_refuses_a_non_mapping_document(tmp_path: Path) -> None:
    workspace = _write_config(tmp_path, "- just\n- a list\n")

    with pytest.raises(SystemExit, match="not a mapping"):
        probe.load_quick_filters(workspace)


def test_load_quick_filters_refuses_a_scalar_exclusion_list(tmp_path: Path) -> None:
    # A bare string is truthy and iterable, so it would be matched character by
    # character instead of keyword by keyword.
    workspace = _write_config(tmp_path, "quick_filters:\n  title_exclude: Backend\n")

    with pytest.raises(SystemExit, match="non-empty list"):
        probe.load_quick_filters(workspace)


def test_load_quick_filters_refuses_a_blank_exclusion_entry(tmp_path: Path) -> None:
    workspace = _write_config(tmp_path, "quick_filters:\n  title_exclude:\n    - '  '\n")

    with pytest.raises(SystemExit, match="non-empty list"):
        probe.load_quick_filters(workspace)


def test_confirmation_population_includes_both_exclude_and_include_miss(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    # Cut by an exclude keyword — confirmation can cancel it.
    repository.create(JobRecord("wanted", "1", "Synthetic Co", "Synthetic Excluded Backend Engineer"), jd_markdown="# JD")
    # Cut by an exclude keyword with no backend token — still in the population,
    # because confirmation runs on the JD body, not on the title.
    repository.create(JobRecord("wanted", "2", "Synthetic Co", "Synthetic Excluded Designer"), jd_markdown="# JD")
    # Not cut at all — not in the population.
    repository.create(JobRecord("wanted", "3", "Synthetic Co", "Backend Engineer"), jd_markdown="# JD")
    # Cut by a seniority keyword — excluded from the population.
    repository.create(JobRecord("wanted", "4", "Synthetic Co", "신입 Backend Engineer"), jd_markdown="# JD")
    # position='legacy' — excluded (broken title filter, separate migration).
    repository.create(JobRecord("wanted", "5", "Synthetic Co", "legacy"), jd_markdown="# JD")
    # Title_include miss (no exclude keyword matches, but title_include is not met).
    repository.create(JobRecord("wanted", "6", "Synthetic Co", "Software Engineer"), jd_markdown="# JD")

    keys = probe.confirmation_population(
        repository, {"title_exclude": ["Synthetic Excluded", "신입"], "title_include": ["Backend", "백엔드", "Server", "서버"]}
    )

    assert set(keys) == {JobKey("wanted", "1"), JobKey("wanted", "2"), JobKey("wanted", "6")}


def test_matching_parents_reports_only_the_matching_items() -> None:
    jd = (
        "# 합성 공고\n\n"
        "## 자격요건\n\n"
        "- Python 기반 API 서버 개발 경험\n"
        "- 브라우저 렌더링 성능 최적화 경험\n"
    )

    parents, matches = probe.matching_parents(jd)

    assert parents == 2
    assert [item.text for item in matches] == ["Python 기반 API 서버 개발 경험"]


def test_matching_parents_reports_zero_for_an_unparseable_body() -> None:
    parents, matches = probe.matching_parents("# 합성 공고\n\n## 회사 소개\n\n- API 서버를 운영합니다\n")

    assert (parents, matches) == (0, [])


def test_matching_parents_finds_a_nested_requirement() -> None:
    # The outer bullet is generic and the backend evidence is indented under it.
    # Scanning parents alone would set this posting aside.
    jd = (
        "# 합성 공고\n\n"
        "## 자격요건\n\n"
        "- 아래 항목 중 하나 이상에 해당하는 분\n"
        "  - 합성 프레임워크 기반 백엔드 서비스 운영 경험\n"
        "- 동료 리뷰 참여 경험\n"
    )

    _, matches = probe.matching_parents(jd)

    assert [item.text for item in matches] == ["합성 프레임워크 기반 백엔드 서비스 운영 경험"]


def _probe_workspace(tmp_path: Path, monkeypatch, exclude: str) -> JDRecordRepository:
    config_dir = tmp_path / "private/jd/config"
    config_dir.mkdir(parents=True)
    (config_dir / "search_config.yaml").write_text(
        f"quick_filters:\n  title_exclude:\n    - {exclude}\n", encoding="utf-8"
    )
    records = tmp_path / "private/jd/records"
    repository = JDRecordRepository(records)

    class _WS:
        jobs_config_dir = config_dir
        jobs_records_dir = records

    monkeypatch.setattr(probe, "resolve_workspace", lambda: _WS())
    return repository


def test_main_reports_a_confirmed_record_and_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    repository = _probe_workspace(tmp_path, monkeypatch, "Synthetic Excluded")
    repository.create(
        JobRecord("wanted", "1", "Synthetic Co", "Synthetic Excluded Backend Engineer"),
        jd_markdown="# 합성 공고\n\n## 자격요건\n\n- Python 기반 API 서버 개발 경험\n",
    )

    exit_code = probe.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "CONFIRMED" in out
    assert "disputed records derived: 1" in out
    assert "confirmed by requirements: 1" in out
    assert "failures: 0" in out


def test_main_reports_an_empty_manifest_as_set_aside_not_a_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repository = _probe_workspace(tmp_path, monkeypatch, "Synthetic Excluded")
    repository.create(
        JobRecord("wanted", "1", "Synthetic Co", "Synthetic Excluded Backend Engineer"),
        jd_markdown="# 합성 공고\n\n## 회사 소개\n\n- API 서버를 운영합니다\n",
    )

    exit_code = probe.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "SET ASIDE — empty manifest" in out
    assert "set aside, empty manifest: 1" in out


def test_main_refuses_an_expected_count_that_does_not_match(tmp_path: Path, monkeypatch) -> None:
    repository = _probe_workspace(tmp_path, monkeypatch, "Synthetic Excluded")
    repository.create(
        JobRecord("wanted", "1", "Synthetic Co", "Synthetic Excluded Backend Engineer"),
        jd_markdown="# JD\n",
    )

    with pytest.raises(SystemExit, match="derived 1 confirmation candidates, expected 5"):
        probe.main(["--expect-disputed", "5"])


def test_main_refuses_an_empty_derived_set(tmp_path: Path, monkeypatch) -> None:
    # Nothing the title filter cuts, so the confirmation under test never runs.
    repository = _probe_workspace(tmp_path, monkeypatch, "Synthetic Excluded")
    repository.create(JobRecord("wanted", "1", "Synthetic Co", "Backend Engineer"), jd_markdown="# JD\n")

    with pytest.raises(SystemExit, match="confirmation population is empty"):
        probe.main([])
