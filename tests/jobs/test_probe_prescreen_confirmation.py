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

    with pytest.raises(SystemExit, match="title_exclude is empty"):
        probe.load_quick_filters(workspace)


def test_load_quick_filters_refuses_a_missing_section(tmp_path: Path) -> None:
    workspace = _write_config(tmp_path, "search:\n  role: backend\n")

    with pytest.raises(SystemExit, match="quick_filters missing"):
        probe.load_quick_filters(workspace)


def test_disputed_titles_derives_the_cut_backend_titles(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    # Cut by the exclude keyword, but the title still says backend — the disputed set.
    repository.create(JobRecord("wanted", "1", "Synthetic Co", "Synthetic Excluded Backend Engineer"), jd_markdown="# JD")
    # Cut by the same keyword with no backend token — not disputed, the title stands.
    repository.create(JobRecord("wanted", "2", "Synthetic Co", "Synthetic Excluded Designer"), jd_markdown="# JD")
    # Not cut at all.
    repository.create(JobRecord("wanted", "3", "Synthetic Co", "Backend Engineer"), jd_markdown="# JD")

    keys = probe.disputed_titles(repository, {"title_exclude": ["Synthetic Excluded"]})

    assert keys == [JobKey("wanted", "1")]


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
