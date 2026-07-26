from __future__ import annotations

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.status import get_status, normalize_status, parse_frontmatter
from careerkit.jobs.domain.model import ApplicationStatus, JobRecord, ScreeningVerdict
from careerkit.jobs.domain.naming import normalize_company_name
from careerkit.jobs.domain.verdict import classify_by_verdict, parse_verdict_from_screening


def test_parse_frontmatter_and_status_normalization() -> None:
    content = "---\nstatus: rejected\nstatus_updated: 2026-01-24\n---\n# JD 내용"
    result = parse_frontmatter(content)
    assert result["status"] == "rejected"
    assert normalize_status("패스") == "rejected"
    assert normalize_status("조건부(하)") == "pending"


def test_verdict_and_company_normalization_regressions() -> None:
    content = "## 최종 판정\n\n| 포지션 | 판정 | 사유 |\n|--------|------|------|\n| A | 🟡 지원 보류 | 조건부 |\n| B | 🔴 지원 비추천 | 리드 역할 |\n"
    assert parse_verdict_from_screening(content) == "지원 비추천"
    assert classify_by_verdict("강력 추천") == "conditional/high"
    assert normalize_company_name("(주)샘플컴퍼니") == "샘플컴퍼니"
    assert normalize_company_name("ACME Corp.") == "acme"


def test_get_status_uses_independent_axes(tmp_path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    repository.create(
        JobRecord("wanted", "123", "TestCo", "Backend", screening_verdict=ScreeningVerdict.RECOMMENDED),
        jd_markdown="# JD",
    )
    repository.create(
        JobRecord("remember", "456", "RejectCo", "Backend", application_status=ApplicationStatus.REJECTED),
        jd_markdown="# JD",
    )

    status = get_status(repository=repository)

    assert status["screening:recommended"] == 1
    assert status["screening:unscreened"] == 1
    assert status["application:pending"] == 1
    assert status["application:rejected"] == 1
    assert status["posting:active"] == 2
