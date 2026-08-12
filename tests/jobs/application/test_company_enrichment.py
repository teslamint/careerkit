from __future__ import annotations

from pathlib import Path

import pytest

from careerkit.jobs.application import company_info as company_info_mod
from careerkit.jobs.application.company_enrichment import (
    CompanyEnrichmentContext,
    CompanyEnrichmentService,
    CompanyInfoEnrichmentResult,
)
from careerkit.jobs.application.company_info import CompanyInfoService
from careerkit.workspace import WorkspacePaths


def test_enrichment_writes_ready_markdown_for_missing_lookup(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="remember",
            item_id="remember:1",
            company_name="ReadyCo",
            company_id="101",
            source_url="https://example.com/jobs/1",
            facts={
                "industry": "IT",
                "founded_year": 2020,
                "employee_current": 45,
                "employee_joined_1y": 9,
                "employee_left_1y": 2,
            },
            fact_sources={
                "industry": ("https://example.com/company/101",),
                "founded_year": ("https://example.com/company/101",),
                "employee_current": ("https://example.com/company/101",),
                "employee_joined_1y": ("https://example.com/company/101",),
                "employee_left_1y": ("https://example.com/company/101",),
            },
        )
    )

    assert result.status == "ready"
    assert result.attempted is True
    assert result.persisted is True
    assert result.warning_code is None
    assert result.completeness == 100.0
    assert result.file_path == tmp_path / "private" / "company_info" / "readyco.md"
    lookup = service.company_info.inspect("ReadyCo")
    assert lookup.status == "ready"
    assert lookup.validation is not None
    assert lookup.validation.completeness_score == 100.0


def test_enrichment_preserves_non_empty_facts_and_returns_below_threshold_warning(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    company_file.write_text(
        "# Acme\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 스타트업 여부 | yes |\n"
        "| 설립 | 2021년 |\n"
        "| 직원수 | 11명 |\n\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n",
        encoding="utf-8",
    )
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="remember",
            item_id="remember:2",
            company_name="Acme",
            company_id="102",
            source_url="https://example.com/jobs/2",
            facts={
                "employee_current": 999,
                "industry": "SaaS",
            },
            fact_sources={
                "employee_current": ("https://example.com/company/102",),
                "industry": ("https://example.com/company/102",),
            },
        )
    )

    assert result.status == "warning"
    assert result.attempted is True
    assert result.persisted is True
    assert result.warning_code == "below_threshold"
    assert result.completeness == pytest.approx(33.33333333333333)
    updated = company_file.read_text(encoding="utf-8")
    assert "| 직원수 | 11명 |" in updated
    assert "| 업종 | SaaS |" in updated
    assert "https://example.com/company/102" in updated


def test_enrichment_skips_unsourced_new_facts(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:3",
            company_name="SourceCo",
            company_id="103",
            source_url="https://example.com/jobs/3",
            facts={
                "industry": "AI",
                "founded_year": 2021,
                "employee_current": 15,
            },
            fact_sources={
                "industry": (),
                "founded_year": ("https://example.com/company/103",),
                "employee_current": (),
            },
        )
    )

    assert result.status == "warning"
    saved = (tmp_path / "private" / "company_info" / "sourceco.md").read_text(encoding="utf-8")
    assert "| 업종 | AI |" not in saved
    assert "| 설립 | 2021년 |" in saved
    assert "| 직원수 | 15명 |" not in saved
    assert "https://example.com/company/103" in saved


def test_enrichment_keeps_only_mixed_source_fields_with_urls(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="remember",
            item_id="remember:4",
            company_name="MixedCo",
            company_id="104",
            source_url="https://example.com/jobs/4",
            facts={
                "industry": "Fintech",
                "employee_current": 30,
                "employee_joined_1y": 8,
                "employee_left_1y": 1,
            },
            fact_sources={
                "industry": (),
                "employee_current": ("https://example.com/company/104",),
                "employee_joined_1y": ("https://example.com/company/104",),
                "employee_left_1y": (),
            },
        )
    )

    assert result.status == "warning"
    saved = (tmp_path / "private" / "company_info" / "mixedco.md").read_text(encoding="utf-8")
    assert "| 업종 | Fintech |" not in saved
    assert "| 직원수 | 30명 |" in saved
    assert "| 1년간 입사자 | 8명 |" in saved
    assert "| 1년간 퇴사자 | 1명 |" not in saved


def test_enrichment_dry_run_computes_ready_result_without_writing_file(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="groupby",
            item_id="groupby:456",
            company_name="DryRun Co",
            company_id="456",
            source_url="https://groupby.kr/positions/456",
            facts={
                "industry": "SaaS",
                "founded_year": 2020,
                "employee_current": 40,
                "employee_joined_1y": 8,
                "employee_left_1y": 2,
                "investment_round": "Series A",
                "investment_total": 120.0,
                "is_startup": True,
            },
            fact_sources={
                "industry": ("https://groupby.kr/positions/456",),
                "founded_year": ("https://groupby.kr/positions/456",),
                "employee_current": ("https://groupby.kr/positions/456",),
                "employee_joined_1y": ("https://groupby.kr/positions/456",),
                "employee_left_1y": ("https://groupby.kr/positions/456",),
                "investment_round": ("https://groupby.kr/positions/456",),
                "investment_total": ("https://groupby.kr/positions/456",),
                "is_startup": ("https://groupby.kr/positions/456",),
            },
        ),
        dry_run=True,
    )

    assert result == CompanyInfoEnrichmentResult(
        status="ready",
        attempted=True,
        persisted=False,
        completeness=100.0,
        warning_code=None,
        file_path=None,
    )
    assert not (tmp_path / "private" / "company_info" / "dryrun-co.md").exists()


def test_enrichment_renders_avg_salary_into_markdown(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:5",
            company_name="SalaryCo",
            company_id="500",
            source_url="https://www.wanted.co.kr/wd/5",
            facts={
                "industry": "IT",
                "founded_year": 2019,
                "employee_current": 20,
                "avg_salary": 5600,
                "is_startup": True,
            },
            fact_sources={
                "industry": ("https://www.wanted.co.kr/company/500",),
                "founded_year": ("https://www.wanted.co.kr/company/500",),
                "employee_current": ("https://www.wanted.co.kr/company/500",),
                "avg_salary": ("https://www.wanted.co.kr/company/500",),
                "is_startup": ("https://www.wanted.co.kr/company/500",),
            },
        )
    )

    saved = (tmp_path / "private" / "company_info" / "salaryco.md").read_text(encoding="utf-8")
    assert "**5,600만원**" in saved
    assert "## 연봉 정보" in saved

    from careerkit.jobs.application.company_info import parse_company_file

    parsed = parse_company_file(tmp_path / "private" / "company_info" / "salaryco.md")
    assert parsed.avg_salary == 5600


def test_enrichment_renders_revenue_into_markdown_and_round_trips(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:6",
            company_name="RevenueCo",
            company_id="600",
            source_url="https://www.wanted.co.kr/wd/6",
            facts={
                "founded_year": 2020,
                "employee_current": 30,
                "revenue": 38.3,
            },
            fact_sources={
                "founded_year": ("https://www.wanted.co.kr/company/600",),
                "employee_current": ("https://www.wanted.co.kr/company/600",),
                "revenue": ("https://www.wanted.co.kr/company/600",),
            },
        )
    )

    saved = (tmp_path / "private" / "company_info" / "revenueco.md").read_text(encoding="utf-8")
    assert "## 매출 정보" in saved
    assert "| 매출액 | 38.3억원 |" in saved

    from careerkit.jobs.application.company_info import parse_company_file

    parsed = parse_company_file(tmp_path / "private" / "company_info" / "revenueco.md")
    assert parsed.revenue == 38.3


def test_enrichment_upserts_revenue_section_into_existing_incomplete_markdown(
    tmp_path: Path,
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "revenueco.md"
    company_file.write_text(
        "# RevenueCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "## 회사 문화\n\n"
        "이 문단은 유지되어야 합니다.\n\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n",
        encoding="utf-8",
    )
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:6",
            company_name="RevenueCo",
            company_id="600",
            source_url="https://www.wanted.co.kr/wd/6",
            facts={
                "employee_current": 30,
                "revenue": 38.3,
            },
            fact_sources={
                "employee_current": ("https://www.wanted.co.kr/company/600",),
                "revenue": ("https://www.wanted.co.kr/company/600",),
            },
        )
    )

    assert result.status == "ready"
    saved = company_file.read_text(encoding="utf-8")
    assert saved.count("## 매출 정보") == 1
    assert saved.index("## 매출 정보") < saved.index("\n---\n")
    assert "## 회사 문화" in saved
    assert "이 문단은 유지되어야 합니다." in saved
    assert "## 매출 정보" in saved
    assert "| 매출액 | 38.3억원 |" in saved


def test_enrichment_ignores_unknown_location_fact_and_preserves_existing_supported_facts(
    tmp_path: Path,
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "groupbyco.md"
    company_file.write_text(
        "# GroupByCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 업종 | 헬스케어 |\n"
        "| 설립 | 2021년 |\n"
        "| 직원수 | 11명 |\n\n"
        "## 투자 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 현재 라운드 | Series A |\n\n"
        "---\n\n"
        "*출처:*\n- https://groupby.kr/positions/1\n",
        encoding="utf-8",
    )
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    result = service.enrich(
        CompanyEnrichmentContext(
            platform="groupby",
            item_id="groupby:1",
            company_name="GroupByCo",
            company_id=None,
            source_url="https://groupby.kr/positions/1",
            facts={
                "industry": "AI",
                "employee_current": 24,
                "investment_round": "Series B",
                "location": "서울",
                "avg_salary": 4500,
            },
            fact_sources={
                "industry": ("https://www.wanted.co.kr/company/12345",),
                "employee_current": ("https://www.wanted.co.kr/company/12345",),
                "investment_round": ("https://www.wanted.co.kr/company/12345",),
                "location": ("https://www.wanted.co.kr/company/12345",),
                "avg_salary": ("https://www.wanted.co.kr/company/12345",),
            },
        )
    )

    assert result.status == "warning"
    updated = company_file.read_text(encoding="utf-8")
    assert "| 업종 | 헬스케어 |" in updated
    assert "| 직원수 | 11명 |" in updated
    assert "| 현재 라운드 | Series A |" in updated
    assert "| 평균 연봉 | **4,500만원** |" in updated
    assert "서울" not in updated


def test_enrichment_renders_revenue_once_before_footer_for_new_file(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:7",
            company_name="SingleRevenueCo",
            company_id="700",
            source_url="https://www.wanted.co.kr/wd/7",
            facts={
                "founded_year": 2020,
                "employee_current": 30,
                "revenue": 55.5,
            },
            fact_sources={
                "founded_year": ("https://www.wanted.co.kr/company/700",),
                "employee_current": ("https://www.wanted.co.kr/company/700",),
                "revenue": ("https://www.wanted.co.kr/company/700",),
            },
        )
    )

    saved = (tmp_path / "private" / "company_info" / "singlerevenueco.md").read_text(encoding="utf-8")
    assert saved.count("## 매출 정보") == 1
    assert saved.index("## 매출 정보") < saved.index("\n---\n")


def test_enrichment_validation_rejection_preserves_prior_bytes_and_digest(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "guardedco.md"
    original = (
        "# GuardedCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n"
    )
    company_file.write_text(original, encoding="utf-8")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))
    before = service.company_info.inspect("GuardedCo")

    def fail_validate(_data, _path):
        raise ValueError("invalid generated markdown")

    original_validate_company = company_info_mod.validate_company
    monkeypatch.setattr(company_info_mod, "validate_company", fail_validate)

    with pytest.raises(ValueError, match="invalid generated markdown"):
        service.enrich(
            CompanyEnrichmentContext(
                platform="wanted",
                item_id="wanted:8",
                company_name="GuardedCo",
                company_id="800",
                source_url="https://www.wanted.co.kr/wd/8",
                facts={"employee_current": 30, "revenue": 38.3},
                fact_sources={
                    "employee_current": ("https://www.wanted.co.kr/company/800",),
                    "revenue": ("https://www.wanted.co.kr/company/800",),
                },
            )
        )

    monkeypatch.setattr(company_info_mod, "validate_company", original_validate_company)
    after = service.company_info.inspect("GuardedCo")
    assert company_file.read_text(encoding="utf-8") == original
    assert before.digest == after.digest


def test_enrichment_replace_failure_preserves_prior_bytes_and_digest(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "replaceco.md"
    original = (
        "# ReplaceCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n"
    )
    company_file.write_text(original, encoding="utf-8")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))
    before = service.company_info.inspect("ReplaceCo")

    monkeypatch.setattr(company_info_mod.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError, match="replace failed"):
        service.enrich(
            CompanyEnrichmentContext(
                platform="wanted",
                item_id="wanted:9",
                company_name="ReplaceCo",
                company_id="900",
                source_url="https://www.wanted.co.kr/wd/9",
                facts={"employee_current": 30, "revenue": 38.3},
                fact_sources={
                    "employee_current": ("https://www.wanted.co.kr/company/900",),
                    "revenue": ("https://www.wanted.co.kr/company/900",),
                },
            )
        )

    after = service.company_info.inspect("ReplaceCo")
    assert company_file.read_text(encoding="utf-8") == original
    assert before.digest == after.digest


def test_enrichment_fsync_file_failure_can_leave_valid_new_file(tmp_path: Path, monkeypatch) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    monkeypatch.setattr(company_info_mod, "_fsync_file", lambda _path: (_ for _ in ()).throw(OSError("fsync file failed")))

    with pytest.raises(OSError, match="fsync file failed"):
        service.enrich(
            CompanyEnrichmentContext(
                platform="wanted",
                item_id="wanted:10",
                company_name="FsyncFileCo",
                company_id="1000",
                source_url="https://www.wanted.co.kr/wd/10",
                facts={"founded_year": 2020, "employee_current": 30, "revenue": 38.3},
                fact_sources={
                    "founded_year": ("https://www.wanted.co.kr/company/1000",),
                    "employee_current": ("https://www.wanted.co.kr/company/1000",),
                    "revenue": ("https://www.wanted.co.kr/company/1000",),
                },
            )
        )

    lookup = service.company_info.inspect("FsyncFileCo")
    assert lookup.status == "ready"


def test_enrichment_fsync_directory_failure_can_leave_valid_new_file_and_next_inspect_compensates(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    service = CompanyEnrichmentService(company_info=CompanyInfoService(workspace=workspace))

    monkeypatch.setattr(company_info_mod, "_fsync_directory", lambda _path: (_ for _ in ()).throw(OSError("fsync dir failed")))

    with pytest.raises(OSError, match="fsync dir failed"):
        service.enrich(
            CompanyEnrichmentContext(
                platform="wanted",
                item_id="wanted:11",
                company_name="FsyncDirCo",
                company_id="1100",
                source_url="https://www.wanted.co.kr/wd/11",
                facts={"founded_year": 2020, "employee_current": 30, "revenue": 38.3},
                fact_sources={
                    "founded_year": ("https://www.wanted.co.kr/company/1100",),
                    "employee_current": ("https://www.wanted.co.kr/company/1100",),
                    "revenue": ("https://www.wanted.co.kr/company/1100",),
                },
            )
        )

    retry = service.enrich(
        CompanyEnrichmentContext(
            platform="wanted",
            item_id="wanted:11",
            company_name="FsyncDirCo",
            company_id="1100",
            source_url="https://www.wanted.co.kr/wd/11",
            facts={"founded_year": 2020, "employee_current": 30, "revenue": 38.3},
            fact_sources={
                "founded_year": ("https://www.wanted.co.kr/company/1100",),
                "employee_current": ("https://www.wanted.co.kr/company/1100",),
                "revenue": ("https://www.wanted.co.kr/company/1100",),
            },
        )
    )

    assert retry.status == "ready"
    assert retry.attempted is False
