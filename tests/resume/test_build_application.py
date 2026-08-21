from __future__ import annotations

import json
from pathlib import Path

from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from careerkit.resume.application.build import ResumeBuildService
from careerkit.resume.application.career_description import build_career
from careerkit.resume.domain.content import calculate_tenure


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_service(tmp_path: Path, config: dict | None = None) -> ResumeBuildService:
    _write_json(
        tmp_path / "variant_config.json",
        config or {
            "job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}, "include_awards": True, "include_languages": True},
            "public": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}, "include_awards": False, "include_languages": False},
        },
    )
    return ResumeBuildService(ResumeWorkspaceAdapter(base_dir=tmp_path))


def test_calculate_tenure_preserves_duration_only_mode() -> None:
    assert calculate_tenure("2020.09 ~ 2022.09", separator="~", include_period=False, error_value="") == "2년 1개월"


def test_build_profile_loads_enabled_sections_in_order(tmp_path: Path) -> None:
    service = _make_service(tmp_path, {"job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}, "include_awards": False, "include_languages": True}, "public": {"companies": []}})
    _write(tmp_path / "profile" / "contact.md", "# Contact\n")
    _write(tmp_path / "profile" / "summary-job.md", "# Summary\n")
    _write(tmp_path / "profile" / "skills-job.md", "# Skills\n")
    _write(tmp_path / "profile" / "education.md", "# Education\n")
    _write(tmp_path / "profile" / "awards.md", "# Awards\n")
    _write(tmp_path / "profile" / "languages.md", "# Languages\n")
    assert service.build_profile("job") == ["# Contact\n", "# Summary\n", "# Skills\n", "# Education\n", "# Languages\n"]


def test_missing_variant_config_has_actionable_error(tmp_path: Path) -> None:
    adapter = ResumeWorkspaceAdapter(base_dir=tmp_path)

    try:
        adapter.load_variant_config()
    except ValueError as exc:
        assert "cp" not in str(exc)
        assert "variant_config.example.json" in str(exc)
        assert "private/variant_config.json" in str(exc)
    else:
        raise AssertionError("missing variant config should fail")


def test_build_company_summary_uses_overview_only(tmp_path: Path) -> None:
    service = _make_service(tmp_path, {"job": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}}, "public": {"companies": []}})
    _write(tmp_path / "companies" / "Acme" / "profile.md", "# Acme\n\n## Overview\n\nOverview line\n\n## Details\n\nHidden details\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "api.md", "## API\n\nHidden\n")
    assert service.build_company(tmp_path / "companies" / "Acme", "job") == ["# Acme\n\nOverview line"]


def test_build_career_uses_configured_target_and_variant(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "variant_config.json",
        {
            "job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}},
            "public": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}},
        },
    )
    _write(tmp_path / "profile" / "contact.md", "# Contact\n\n- Name: 홍길동\n- Email: test@example.com\n- Phone: 010-1234-5678\n")
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n\nBase narrative\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "api.md",
        "# Base Project\n\n## Summary\n\nBase context\n",
    )
    _write(
        tmp_path / "companies" / "Other" / "profile.md",
        "# Other\n\n## Overview\n- Period: 2022.01 - 2023.01\n- Role: Engineer\n",
    )
    _write_json(
        tmp_path / "overrides" / "target-one" / "config.json",
        {"job": {"companies": ["Acme"], "company_detail": {"Acme": "full"}}},
    )
    _write(
        tmp_path / "overrides" / "target-one" / "companies" / "Acme" / "profile.md",
        "# Acme Target\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n\n"
        "<!-- job-only:start -->\nTarget job narrative\n<!-- job-only:end -->\n"
        "<!-- public-only:start -->\nTarget public narrative\n<!-- public-only:end -->\n",
    )
    _write(
        tmp_path / "overrides" / "target-one" / "companies" / "Acme" / "projects" / "api.md",
        "# Target Project\n\n## Summary\n\nTarget context\n\n## Responsibilities\n\n- Built target API\n",
    )

    result = build_career(ResumeWorkspaceAdapter(base_dir=tmp_path, target="target-one"), "job")
    public_result = build_career(ResumeWorkspaceAdapter(base_dir=tmp_path, target="target-one"), "public")

    assert "이름: 홍길동" in result
    assert "Acme Target" in result
    assert "Target job narrative" in result
    assert "Target Project" in result
    assert "Built target API" in result
    for excluded in ("Base narrative", "Base Project", "Target public narrative", "Other"):
        assert excluded not in result
    assert "Target public narrative" in public_result
    assert "Target job narrative" not in public_result


def test_build_career_summary_uses_filtered_overview_and_omits_projects(tmp_path: Path) -> None:
    service = _make_service(
        tmp_path,
        {
            "job": {"companies": ["Acme"], "company_detail": {"Acme": "summary"}},
            "public": {"companies": []},
        },
    )
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n- Employment: Full-time\n- Position: Senior\n- Department: Platform\n\n## Summary\n\nOwned backend systems.\n\n## Key Responsibilities\n\n- Operated APIs\n\n## Tech Stack\n\n- Python\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "api.md",
        "# API\n\n## Key Responsibilities\n\n- Built APIs\n\n## Achievements\n\n- Reduced latency\n",
    )

    result = build_career(service.adapter, "job")

    for expected in ("고용형태: Full-time", "직급: Senior", "부서: Platform", "담당업무: Owned backend systems."):
        assert expected in result
    for excluded in ("프로젝트 1", "Built APIs", "Reduced latency", "주요 책임:", "기술스택: Python"):
        assert excluded not in result


def test_build_career_uses_context_fallbacks_and_omits_title_only_projects(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n"
        "- Period: 2020.01 - 2022.01\n"
        "- Role: Engineer\n"
        "- Department: Platform\n"
        "- 부서: 제품\n\n"
        "Overview narrative\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "01-summary.md",
        "# Summary Project\n\n## Summary\n\nSummary context\n\n## Key Responsibilities\n\n- Owned API\n\n## Achievements\n\n- Improved latency\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "02-overview.md",
        "# Overview Project\n\n## Overview\n"
        "- Period: 2021.01 - 2021.12\n"
        "- Type: Backend\n"
        "- Tech Stack: Python\n\n"
        "Overview context\n\n## Responsibilities\n\n- Built API\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "03-legacy.md",
        "# Legacy Project\n\n## Period\n\n2020.06 - 2020.12\n\n## Achievements\n\n- Shipped migration\n",
    )
    _write(tmp_path / "companies" / "Acme" / "projects" / "04-empty.md", "# Empty Project\n")

    result = build_career(service.adapter, "job")

    for expected in (
        "담당업무: Overview narrative",
        "개요: Summary context",
        "상세 업무:",
        "Owned API",
        "성과:",
        "Improved latency",
        "개요: Overview context",
        "Built API",
        "기간: 2020.06 - 2020.12",
        "Shipped migration",
    ):
        assert expected in result
    for excluded in (
        "고용형태:",
        "Department: Platform",
        "부서: 제품",
        "Type: Backend",
        "Tech Stack: Python",
        "Empty Project",
    ):
        assert excluded not in result


def test_build_career_empty_configured_companies_keeps_empty_message(tmp_path: Path) -> None:
    service = _make_service(tmp_path, {"job": {"companies": [], "company_detail": {}}, "public": {"companies": []}})
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )

    result = build_career(service.adapter, "job")

    assert "(등록된 경력 없음)" in result
    assert "Acme" not in result


def test_wanted_and_job_pdf_builds_keep_detailed_sections(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _write(tmp_path / "profile" / "contact.md", "# Contact\n- Name: User\n- Email: user@example.com\n- Phone: 010\n- GitHub: github.com/user\n")
    _write(tmp_path / "profile" / "summary-job.md", "# Summary\n\nSummary body\n")
    _write(tmp_path / "profile" / "skills-job.md", "# Skills\n\n- Python (advanced)\n")
    _write(tmp_path / "profile" / "education.md", "# Education\n\n## School\n- Period: 2010 - 2014\n- Major: CS\n- Status: Graduated\n")
    _write(tmp_path / "profile" / "awards.md", "# Awards\n\n## Prize\n- Period: 2020\n- Description: Winner\n")
    _write(tmp_path / "profile" / "languages.md", "# Languages\n\n- Korean\n")
    _write(tmp_path / "companies" / "Acme" / "profile.md", "# Acme\n\n## Overview\n\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n- Employment: Full-time\n\n## Summary\n\nCompany summary\n\n## Key Responsibilities\n\n- Owned APIs\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "api.md", "# API Project\n\n## Overview\n\n- Period: 2021\n\n## Tech Stack\n\n- FastAPI\n\n## Responsibilities\n\n- Built API\n\n## Achievements\n\n- Improved latency\n")

    wanted = service.build_wanted("job")
    pdf = service.build_full_pdf("job")

    for expected in ("Company summary", "Owned APIs", "API Project", "FastAPI", "학력", "School", "CS", "스킬", "Python", "Prize", "Winner", "Korean"):
        assert expected in wanted
    assert "# Contact" not in pdf
    assert "# Summary" in pdf
    assert "# Skills" in pdf
    assert "# Experience" in pdf
    assert "# Education" in pdf
    assert "# Education" in service.build_short_pdf("job")
    assert "# Links" in pdf


def test_job_short_pdf_filters_education_variant_tags(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _write(
        tmp_path / "profile" / "education.md",
        "# Education\n"
        "<!-- public-only:start -->\n## Public School\n- Major: Public\n"
        "<!-- public-only:end -->\n"
        "<!-- job-only:start -->\n## Job School\n- Major: Job\n"
        "<!-- job-only:end -->\n",
    )

    result = service.build_short_pdf("job")

    assert "Job School | Job" in result
    assert "Public School" not in result


def test_wanted_project_uses_legacy_period_section(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    _write(tmp_path / "profile" / "contact.md", "# Contact\n- Name: User\n")
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "legacy.md",
        "# Legacy Project\n\n## Period\n2021.01 - 2021.12\n",
    )

    result = service.build_wanted("job")

    assert "Legacy Project\n2021.01 - 2021.12" in result


def test_build_company_dict_detail_selects_and_excludes_projects(tmp_path: Path) -> None:
    service = _make_service(
        tmp_path,
        {
            "job": {
                "companies": ["Acme"],
                "company_detail": {
                    "Acme": {"level": "full", "projects": ["api", "platform"], "exclude_projects": ["legacy"]}
                },
            },
            "public": {"companies": []},
        },
    )
    _write(tmp_path / "companies" / "Acme" / "profile.md", "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "api.md", "# API\n\nAPI work\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "platform.md", "# Platform\n\nPlatform work\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "legacy.md", "# Legacy\n\nLegacy work\n")
    _write(tmp_path / "companies" / "Acme" / "projects" / "other.md", "# Other\n\nOther work\n")

    result = "\n".join(service.build_company(tmp_path / "companies" / "Acme", "job"))

    assert "API work" in result
    assert "Platform work" in result
    assert "Legacy work" not in result
    assert "Other work" not in result


def test_build_wanted_dict_detail_selects_projects(tmp_path: Path) -> None:
    service = _make_service(
        tmp_path,
        {
            "job": {
                "companies": ["Acme"],
                "company_detail": {"Acme": {"level": "full", "projects": ["api"]}},
            },
            "public": {"companies": []},
        },
    )
    _write(tmp_path / "profile" / "contact.md", "# Contact\n- Name: User\n")
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "api.md",
        "# API Project\n\n## Overview\n- Period: 2021\n\n## Tech Stack\n- FastAPI\n\n## Responsibilities\n- Built API\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "other.md",
        "# Other Project\n\n## Overview\n- Period: 2021\n\n## Tech Stack\n- Python\n\n## Responsibilities\n- Did other\n",
    )

    result = service.build_wanted("job")

    assert "API Project" in result
    assert "FastAPI" in result
    assert "Other Project" not in result


def test_build_career_dict_detail_selects_projects(tmp_path: Path) -> None:
    service = _make_service(
        tmp_path,
        {
            "job": {
                "companies": ["Acme"],
                "company_detail": {"Acme": {"level": "full", "projects": ["api"]}},
            },
            "public": {"companies": []},
        },
    )
    _write(
        tmp_path / "companies" / "Acme" / "profile.md",
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n\n## Summary\nOwned backend.\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "api.md",
        "# API\n\n## Key Responsibilities\n- Built API\n\n## Achievements\n- Improved latency\n",
    )
    _write(
        tmp_path / "companies" / "Acme" / "projects" / "other.md",
        "# Other\n\n## Key Responsibilities\n- Did other\n",
    )

    result = build_career(service.adapter, "job")

    assert "Built API" in result
    assert "Improved latency" in result
    assert "Did other" not in result
