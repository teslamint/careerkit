from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from careerkit.jobs.application.company_info import (
    CompanyData,
    CompanyInfoService,
    RiskFlag,
    ValidationResult,
    add_risk_section_to_markdown,
    parse_company_file,
    validate_company,
)
from careerkit.workspace import WorkspacePaths


FROZEN_NOW = datetime(2026, 1, 15, 9, 30)


def test_parse_company_file_extracts_base_and_startup_fields(tmp_path: Path) -> None:
    company_file = tmp_path / "startup.md"
    company_file.write_text(
        "# 스타트업 (Startup)\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2024년 |\n"
        "| 직원수 | 100명 |\n"
        "| 업종 | IT |\n"
        "| 스타트업 여부 | yes |\n\n"
        "## 인원 통계\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 1년간 입사자 | 70명 |\n"
        "| 1년간 퇴사자 | 55명 |\n\n"
        "## 연봉 정보\n\n"
        "| 항목 | 금액 |\n|------|------|\n"
        "| 평균 연봉 | **5,200만원** |\n"
        "| 상위 | 상위 15% |\n\n"
        "## 투자 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 현재 라운드 | Series B |\n"
        "| 누적 투자금 | 약 130억원 |\n",
        encoding="utf-8",
    )

    data = parse_company_file(company_file)

    assert data.name == "스타트업"
    assert data.name_en == "Startup"
    assert data.founded_year == 2024
    assert data.employee_current == 100
    assert data.employee_joined_1y == 70
    assert data.employee_left_1y == 55
    assert data.avg_salary == 5200
    assert data.salary_percentile == "15"
    assert data.is_startup is True
    assert data.investment_round == "Series B"
    assert data.investment_total == 130.0


def test_validate_company_keeps_legacy_turnover_and_age_logic() -> None:
    data = CompanyData(
        name="TestCo",
        employee_current=100,
        employee_joined_1y=70,
        employee_left_1y=55,
        avg_salary=5000,
        founded_year=2025,
        is_startup=True,
    )

    result = validate_company(data, Path("test.md"), now=FROZEN_NOW)
    codes = {flag.code for flag in result.risk_flags}

    assert "TURNOVER_HIGH" in codes
    assert "EARLY_STAGE" in codes
    assert "NO_INVESTMENT_DATA" in codes


def test_company_info_service_validates_and_applies_risk_fix(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True, exist_ok=True)
    company_file = company_dir / "acme.md"
    company_file.write_text(
        "# Acme\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2021년 |\n"
        "| 직원수 | 100명 |\n"
        "| 업종 | IT |\n\n"
        "## 인원 통계\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 1년간 입사자 | 30명 |\n"
        "| 1년간 퇴사자 | 60명 |\n\n"
        "## 연봉 정보\n\n"
        "| 항목 | 금액 |\n|------|------|\n"
        "| 평균 연봉 | **4,800만원** |\n",
        encoding="utf-8",
    )
    service = CompanyInfoService(workspace=workspace)

    validation = service.validate(file_name="acme.md", fix=False, now=FROZEN_NOW)
    assert validation.processed_files == 1
    assert validation.error_files == 0
    assert validation.fixed_files == ()
    assert validation.results[0].company_name == "Acme"
    assert {flag.code for flag in validation.results[0].risk_flags} >= {"TURNOVER_CRITICAL"}

    fixed = service.validate(file_name="acme.md", fix=True, now=FROZEN_NOW)
    assert fixed.fixed_files == ("acme.md",)
    updated = company_file.read_text(encoding="utf-8")
    assert "## ⚠️ 리스크 플래그" in updated
    assert "TURNOVER_CRITICAL" in updated


def test_company_info_service_rejects_paths_outside_authoritative_directory(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    service = CompanyInfoService(workspace=workspace)

    with pytest.raises(ValueError, match="inside private/company_info"):
        service.validate(file_name=str(outside))


def test_company_info_service_rejects_symlinked_records(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    source = company_dir / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    link = company_dir / "link.md"
    link.symlink_to(source.name)
    service = CompanyInfoService(workspace=workspace)

    with pytest.raises(FileNotFoundError):
        service.validate(file_name="link.md")


def test_company_info_matching_resolves_safe_slug_alias_symlink(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    source = company_dir / "canonical-record.md"
    source.write_text("# Canonical Company\n", encoding="utf-8")
    alias = company_dir / "jd-company-name.md"
    alias.symlink_to(source.name)
    service = CompanyInfoService(workspace=workspace)

    assert service.find_matching_file("JD Company Name") == source.resolve()

    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (company_dir / "outside-alias.md").symlink_to(outside)
    assert service.find_matching_file("Outside Alias") is None


def test_risk_section_uses_injected_time() -> None:
    result = ValidationResult(
        file_path=Path("test.md"),
        company_name="RiskyCo",
        data=CompanyData(name="RiskyCo"),
        risk_flags=(RiskFlag(code="TURNOVER_HIGH", severity="high", message="test"),),
    )

    section = add_risk_section_to_markdown("", result, now=FROZEN_NOW)

    assert "*자동 생성: 2026-01-15*" in section


def test_unlisted_startup_not_misclassified_by_sangjanng(tmp_path: Path) -> None:
    """'비상장' must not trigger the '상장' negative keyword."""
    company_file = tmp_path / "unlisted.md"
    company_file.write_text(
        "# 테스트컴퍼니\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2024년 |\n"
        "| 직원수 | 5명 |\n"
        "| 기업구분 | 스타트업 (비상장) |\n\n"
        "## 투자 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 현재 라운드 | Seed |\n"
        "| 누적 투자금 | 비공개 |\n\n"
        "---\n\n*출처:*\n- https://thevc.kr/testco\n",
        encoding="utf-8",
    )

    data = parse_company_file(company_file)

    assert data.is_startup is True
    assert data.investment_round == "Seed"


def test_explicit_startup_yes_not_overridden_by_negative_keyword(tmp_path: Path) -> None:
    """스타트업 여부: Yes with a negative keyword present must stay True."""
    company_file = tmp_path / "locked.md"
    company_file.write_text(
        "# 대기업계열스타트업\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2023년 |\n"
        "| 직원수 | 20명 |\n"
        "| 스타트업 여부 | Yes |\n\n"
        "## 투자 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 현재 라운드 | Pre-A |\n",
        encoding="utf-8",
    )

    data = parse_company_file(company_file)

    assert data.is_startup is True


def test_listed_company_detected_by_kospi_keyword(tmp_path: Path) -> None:
    company_file = tmp_path / "listed.md"
    company_file.write_text(
        "# 상장사\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2010년 |\n"
        "| 직원수 | 500명 |\n\n"
        "## 태그\n- 코스피\n",
        encoding="utf-8",
    )

    data = parse_company_file(company_file)

    assert data.is_startup is False


def test_negative_keyword_in_body_does_not_override_startup(tmp_path: Path) -> None:
    """'대기업' in company description body must not flip is_startup."""
    company_file = tmp_path / "medistream.md"
    company_file.write_text(
        "# 인티그레이션\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2019년 |\n"
        "| 직원수 | 145명 |\n\n"
        "## 투자 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 현재 라운드 | Series C |\n"
        "| 누적 투자금 | 661억원 |\n\n"
        "## 회사 소개\n\n"
        "스타트업/대기업 출신 인력이 포함되어 있습니다.\n\n"
        "---\n*출처:*\n- https://thevc.kr/integration\n",
        encoding="utf-8",
    )

    data = parse_company_file(company_file)

    assert data.is_startup is True
    assert data.investment_round == "Series C"
