from __future__ import annotations

from datetime import datetime
from pathlib import Path
import stat
import threading
from typing import TypedDict

import pytest

from careerkit.jobs.application.company_info import (
    CompanyData,
    CompanyInfoLookup,
    CompanyInfoService,
    RiskFlag,
    ValidationResult,
    add_risk_section_to_markdown,
    parse_company_file,
    validate_company,
)
from careerkit.workspace import WorkspacePaths


FROZEN_NOW = datetime(2026, 1, 15, 9, 30)


class _StageCapture(TypedDict):
    stage_name: str
    stage_entries: list[str]
    mode: int
    glob: list[str]


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


def test_company_info_inspect_reports_structured_states(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    ready = company_dir / "ready.md"
    ready.write_text(
        "# ReadyCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n"
        "| 직원수 | 40명 |\n\n",
        encoding="utf-8",
    )
    incomplete = company_dir / "incomplete.md"
    incomplete.write_text(
        "# IncompleteCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n",
        encoding="utf-8",
    )
    invalid = company_dir / "invalid.md"
    invalid.write_text("## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside Alias\n", encoding="utf-8")
    (company_dir / "outside-alias.md").symlink_to(outside)
    service = CompanyInfoService(workspace=workspace)

    missing_lookup = service.inspect("MissingCo")
    ready_lookup = service.inspect("ReadyCo")
    incomplete_lookup = service.inspect("IncompleteCo")
    invalid_lookup = service.inspect("invalid")
    unsafe_lookup = service.inspect("Outside Alias")

    assert missing_lookup.status == "missing"
    assert missing_lookup.file_path is None
    assert ready_lookup.status == "ready"
    assert ready_lookup.validation is not None
    assert ready_lookup.validation.completeness_score == pytest.approx(100.0)
    assert ready_lookup.digest
    assert incomplete_lookup.status == "incomplete"
    assert incomplete_lookup.validation is not None
    assert incomplete_lookup.validation.completeness_score == pytest.approx(50.0)
    assert invalid_lookup.status == "invalid"
    assert invalid_lookup.validation is None
    assert unsafe_lookup.status == "unsafe"
    assert unsafe_lookup.file_path is None


def test_apply_candidate_preserves_untouched_sections_byte_for_byte(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    custom_section = "## 회사 문화\n\n이 문단은 유지되어야 합니다.\n"
    company_file.write_text(
        "# Acme\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2021년 |\n\n"
        f"{custom_section}\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n",
        encoding="utf-8",
    )
    service = CompanyInfoService(workspace=workspace)

    result = service.apply_candidate(
        company_name="Acme",
        markdown=(
            "# Acme\n\n"
            "## 기업 정보\n\n"
            "| 항목 | 내용 |\n|------|------|\n"
            "| 설립 | 2021년 |\n"
            "| 직원수 | 12명 |\n\n"
            "---\n\n"
            "*출처:*\n- https://new.example.com\n"
        ),
    )

    updated = company_file.read_text(encoding="utf-8")
    assert result.status == "ready"
    assert custom_section in updated
    assert "| 직원수 | 12명 |" in updated
    assert "https://new.example.com" in updated


def test_apply_candidate_times_out_and_preserves_prior_bytes(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    original = "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 11명 |\n"
    company_file.write_text(original, encoding="utf-8")
    service = CompanyInfoService(workspace=workspace)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        service.apply_candidate(
            company_name="Acme",
            markdown=(
                "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 12명 |\n"
            ),
            before_validate=lambda _path: (entered.set(), release.wait(2.0)),
        )

    worker = threading.Thread(target=hold_lock)
    worker.start()
    assert entered.wait(1.0)

    with pytest.raises(TimeoutError):
        service.apply_candidate(
            company_name="Acme",
            markdown=(
                "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 99명 |\n"
            ),
            timeout=0.05,
        )

    assert company_file.read_text(encoding="utf-8") == original
    release.set()
    worker.join()

    rerun = service.apply_candidate(
        company_name="Acme",
        markdown=(
            "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 13명 |\n"
        ),
    )
    assert rerun.status == "ready"
    assert "13명" in company_file.read_text(encoding="utf-8")


def test_apply_candidate_serializes_three_packaged_writers_with_stable_lock_file(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    company_file.write_text(
        "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 10명 |\n",
        encoding="utf-8",
    )
    service = CompanyInfoService(workspace=workspace)
    lock_path = company_dir / ".acme.lock"
    holder_entered = threading.Event()
    waiter_entered = threading.Event()
    late_entered = threading.Event()
    release_holder = threading.Event()
    release_waiter = threading.Event()
    order: list[str] = []

    def writer(label: str, employee_count: int, entered: threading.Event, released: threading.Event) -> None:
        service.apply_candidate(
            company_name="Acme",
            markdown=(
                "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n"
                f"| 설립 | 2021년 |\n| 직원수 | {employee_count}명 |\n"
            ),
            before_validate=lambda _path: (order.append(label), entered.set(), released.wait(2.0)),
        )

    holder = threading.Thread(target=writer, args=("holder", 11, holder_entered, release_holder))
    waiter = threading.Thread(target=writer, args=("waiter", 12, waiter_entered, release_waiter))
    holder.start()
    assert holder_entered.wait(1.0)
    assert lock_path.exists()

    waiter.start()
    release_holder.set()
    assert waiter_entered.wait(1.0)
    assert order == ["holder", "waiter"]
    assert lock_path.exists()

    late_result: list[CompanyInfoLookup] = []

    def late_writer() -> None:
        late_result.append(
            service.apply_candidate(
                company_name="Acme",
                markdown=(
                    "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 13명 |\n"
                ),
                timeout=1.0,
                before_validate=lambda _path: (order.append("late"), late_entered.set()),
            )
        )

    late = threading.Thread(target=late_writer)
    late.start()
    release_waiter.set()
    holder.join()
    waiter.join()
    late.join()

    assert late_entered.wait(1.0)
    assert late_result[0].status == "ready"
    assert order == ["holder", "waiter", "late"]
    assert lock_path.exists()
    assert "13명" in company_file.read_text(encoding="utf-8")


def test_apply_candidate_uses_hidden_staging_and_cleans_up(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    stale = company_dir / ".acme.stale.tmp"
    stale.write_text("stale", encoding="utf-8")
    service = CompanyInfoService(workspace=workspace)
    seen: _StageCapture = {
        'stage_name': '',
        'stage_entries': [],
        'mode': 0,
        'glob': [],
    }

    def capture(stage_path: Path) -> None:
        stage_entries = sorted(path.name for path in company_dir.iterdir() if path.name.startswith('.'))
        seen['stage_name'] = stage_path.name
        seen['stage_entries'] = stage_entries
        seen['mode'] = stat.S_IMODE(stage_path.stat().st_mode)
        seen['glob'] = [path.name for path in company_dir.glob('*.md')]

    result = service.apply_candidate(
        company_name="Acme",
        markdown=(
            "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 11명 |\n"
        ),
        before_validate=capture,
    )

    assert result.file_path == company_dir / "acme.md"
    assert seen['stage_name'].startswith('.acme.')
    assert seen['stage_name'].endswith('.tmp')
    assert seen['mode'] == 0o600
    assert seen['glob'] == []
    assert all(not name.endswith('.md') for name in seen['stage_entries'])
    assert not stale.exists()
    assert sorted(path.name for path in company_dir.iterdir()) == ['.acme.lock', 'acme.md']


def test_apply_candidate_rejects_digest_conflict_without_overwrite(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    company_file.write_text(
        "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 11명 |\n",
        encoding="utf-8",
    )
    service = CompanyInfoService(workspace=workspace)
    lookup = service.inspect("Acme")
    company_file.write_text(
        "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 12명 |\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="digest"):
        service.apply_candidate(
            company_name="Acme",
            markdown=(
                "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 99명 |\n"
            ),
            expected_digest=lookup.digest,
        )

    assert "12명" in company_file.read_text(encoding="utf-8")


def test_apply_candidate_abort_preserves_prior_bytes(tmp_path: Path) -> None:
    workspace = WorkspacePaths(root=tmp_path, source="explicit")
    company_dir = tmp_path / "private" / "company_info"
    company_dir.mkdir(parents=True)
    company_file = company_dir / "acme.md"
    original = (
        "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 11명 |\n"
    )
    company_file.write_text(original, encoding="utf-8")
    service = CompanyInfoService(workspace=workspace)

    with pytest.raises(RuntimeError, match="abort"):
        service.apply_candidate(
            company_name="Acme",
            markdown=(
                "# Acme\n\n## 기업 정보\n\n| 항목 | 내용 |\n|------|------|\n| 설립 | 2021년 |\n| 직원수 | 99명 |\n"
            ),
            before_validate=lambda _path: (_path.write_text("corrupted", encoding="utf-8"), (_ for _ in ()).throw(RuntimeError("abort write"))),
        )

    assert company_file.read_text(encoding="utf-8") == original
