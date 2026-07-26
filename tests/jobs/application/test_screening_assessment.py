from __future__ import annotations

import json

import pytest

from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
from careerkit.jobs.application.evidence_checks import parse_match_table
from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest
from careerkit.jobs.application.screening_assessment import (
    AssessmentContractError,
    escape_table_cell,
    parse_screening_assessment,
    render_screening_markdown,
)
from careerkit.jobs.domain.model import JobRecord


@pytest.fixture
def atomic_manifest():
    return extract_requirement_manifest(
        """
## 자격 요건
- Python 백엔드 개발 경험

## 주요 업무
- 결제 서비스 운영

## 우대 사항
- Kafka 사용 경험
""".strip()
    )


@pytest.fixture
def composite_manifest():
    return extract_requirement_manifest(
        """
## 자격 요건
- Python / Django

## 주요 업무
- 결제 서비스 운영
""".strip()
    )


@pytest.fixture
def stored_job() -> StoredJobRecord:
    return StoredJobRecord(
        record=JobRecord(
            platform="wanted",
            job_id="100002",
            company="Example",
            position="Backend Engineer",
            source_url="https://example.com/jobs/100002",
        ),
        jd_markdown="# Example JD",
        screening_markdown=None,
    )


def test_parse_screening_assessment_and_render_markdown_round_trip(
    atomic_manifest,
    stored_job: StoredJobRecord,
) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {
                    "id": "required-001",
                    "match": "충족",
                    "evidence": "[source: private/profile/skills-job.md] Python 경험 | FastAPI 운영\n세부 근거",
                },
                {
                    "id": "main_duty-001",
                    "match": "부분",
                    "evidence": "[source: private/companies/acme/projects/project-a.md] 결제 운영 일부 수행",
                },
                {
                    "id": "preferred-001",
                    "match": "없음",
                    "evidence": "관련 근거 없음",
                },
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001", "main_duty-001"],
            "screening_summary": ["필수 경험은 확인됨", "주요업무 직접성은 일부만 확인됨"],
            "reasons": [
                "Python 백엔드 경험이 JD와 직접 맞는다",
                "결제 운영은 일부만 확인된다",
                "Kafka 우대사항 근거는 없다",
            ],
        },
        ensure_ascii=False,
    )

    assessment = parse_screening_assessment(raw, atomic_manifest)

    assert assessment.verdict == "지원 보류"
    assert assessment.decision_basis == ("required-001", "main_duty-001")
    assert assessment.screening_summary == (
        "필수 경험은 확인됨",
        "주요업무 직접성은 일부만 확인됨",
    )
    assert assessment.reasons == (
        "Python 백엔드 경험이 JD와 직접 맞는다",
        "결제 운영은 일부만 확인된다",
        "Kafka 우대사항 근거는 없다",
    )
    assert [match.id for match in assessment.matches] == [
        "required-001",
        "main_duty-001",
        "preferred-001",
    ]
    assert escape_table_cell("A|B\nC") == "A\\|B C"
    assert escape_table_cell("  ") == "확인 필요"

    markdown = render_screening_markdown(stored_job, stored_job.jd_markdown, atomic_manifest, assessment)
    rows, error = parse_match_table(markdown)

    assert error == ""
    assert [row.requirement for row in rows] == [
        "Python 백엔드 개발 경험",
        "결제 서비스 운영",
        "Kafka 사용 경험",
    ]
    assert [row.kind for row in rows] == ["필수", "주요업무", "우대"]
    assert [row.match for row in rows] == ["충족", "부분", "없음"]
    assert rows[0].evidence == "[source: private/profile/skills-job.md] Python 경험 \\| FastAPI 운영 세부 근거"
    assert markdown.splitlines().count("## 최종 판정") == 1
    assert sum(line.startswith("### 최종 판정:") for line in markdown.splitlines()) == 1
    assert "## 기본 정보" in markdown
    assert "## 스크리닝 결과" in markdown
    assert "## 이력/경험 매칭" in markdown
    assert "## 최종 판정" in markdown
    assert "### 최종 판정: 지원 보류" in markdown
    assert "## 핵심 근거" in markdown


def test_render_screening_markdown_aggregates_composite_parent_rows(
    composite_manifest,
    stored_job: StoredJobRecord,
) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {
                    "id": "required-001.1",
                    "match": "충족",
                    "evidence": "[source: private/profile/skills-job.md] Python 서비스 운영",
                },
                {
                    "id": "required-001.2",
                    "match": "없음",
                    "evidence": "Django 직접 경험은 확인되지 않음",
                },
                {
                    "id": "main_duty-001",
                    "match": "충족",
                    "evidence": "[source: private/companies/acme/projects/project-a.md] 결제 운영 경험",
                },
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001"],
            "screening_summary": ["복합 필수요건은 일부만 충족"],
            "reasons": [
                "Python 근거는 있다",
                "Django 근거는 없다",
                "결제 운영 경험은 있다",
            ],
        },
        ensure_ascii=False,
    )

    assessment = parse_screening_assessment(raw, composite_manifest)
    markdown = render_screening_markdown(stored_job, stored_job.jd_markdown, composite_manifest, assessment)
    rows, error = parse_match_table(markdown)

    assert error == ""
    assert [row.requirement for row in rows] == ["Python / Django", "결제 서비스 운영"]
    assert [row.match for row in rows] == ["부분", "충족"]
    assert "Python (충족): [source: private/profile/skills-job.md] Python 서비스 운영" in rows[0].evidence
    assert "Django (없음): Django 직접 경험은 확인되지 않음" in rows[0].evidence


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("```json\n{}\n```", "JSON 객체만 허용됩니다"),
        (
            {
                "schema_version": 2,
                "matches": [{"id": "required-001", "match": "충족", "evidence": "근거"}],
                "verdict": "지원 추천",
                "decision_basis": ["required-001"],
                "screening_summary": ["요약"],
                "reasons": ["이유1", "이유2", "이유3"],
            },
            "schema_version must be 1",
        ),
        (
            {
                "schema_version": 1,
                "matches": [
                    {"id": "required-001", "match": "충족", "evidence": "근거1"},
                    {"id": "required-001", "match": "부분", "evidence": "근거2"},
                    {"id": "main_duty-001", "match": "충족", "evidence": "근거3"},
                    {"id": "preferred-001", "match": "없음", "evidence": "근거4"},
                ],
                "verdict": "지원 보류",
                "decision_basis": ["required-001"],
                "screening_summary": ["요약"],
                "reasons": ["이유1", "이유2", "이유3"],
            },
            "matches must contain each manifest leaf id exactly once",
        ),
        (
            {
                "schema_version": 1,
                "matches": [
                    {"id": "required-001", "match": "충족", "evidence": "근거1"},
                    {"id": "main_duty-001", "match": "충족", "evidence": "근거2"},
                ],
                "verdict": "지원 보류",
                "decision_basis": ["required-001"],
                "screening_summary": ["요약"],
                "reasons": ["이유1", "이유2", "이유3"],
            },
            "matches must contain each manifest leaf id exactly once",
        ),
        (
            {
                "schema_version": 1,
                "matches": [
                    {"id": "required-001", "match": "충족", "evidence": "근거1"},
                    {"id": "main_duty-001", "match": "충족", "evidence": "근거2"},
                    {"id": "preferred-999", "match": "없음", "evidence": "근거3"},
                ],
                "verdict": "지원 보류",
                "decision_basis": ["required-001"],
                "screening_summary": ["요약"],
                "reasons": ["이유1", "이유2", "이유3"],
            },
            "matches must contain each manifest leaf id exactly once",
        ),
        (
            {
                "schema_version": 1,
                "matches": [
                    {"id": "required-001", "match": "애매", "evidence": "근거1"},
                    {"id": "main_duty-001", "match": "충족", "evidence": "근거2"},
                    {"id": "preferred-001", "match": "없음", "evidence": "근거3"},
                ],
                "verdict": "지원 보류",
                "decision_basis": ["required-001"],
                "screening_summary": ["요약"],
                "reasons": ["이유1", "이유2", "이유3"],
            },
            "invalid match value: 애매",
        ),
    ],
)
def test_parse_screening_assessment_rejects_contract_violations(
    atomic_manifest,
    payload,
    reason: str,
) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    with pytest.raises(AssessmentContractError, match=reason):
        parse_screening_assessment(raw, atomic_manifest)


def test_parse_screening_assessment_rejects_non_parent_decision_basis(composite_manifest) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {"id": "required-001.1", "match": "충족", "evidence": "근거1"},
                {"id": "required-001.2", "match": "부분", "evidence": "근거2"},
                {"id": "main_duty-001", "match": "충족", "evidence": "근거3"},
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001.1"],
            "screening_summary": ["요약"],
            "reasons": ["이유1", "이유2", "이유3"],
        },
        ensure_ascii=False,
    )

    with pytest.raises(AssessmentContractError, match="decision_basis must reference manifest parent ids"):
        parse_screening_assessment(raw, composite_manifest)


def test_parse_screening_assessment_normalizes_summary_and_reasons_to_single_lines(
    atomic_manifest,
    stored_job: StoredJobRecord,
) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {"id": "required-001", "match": "충족", "evidence": "근거1"},
                {"id": "main_duty-001", "match": "부분", "evidence": "근거2"},
                {"id": "preferred-001", "match": "없음", "evidence": "근거3"},
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001"],
            "screening_summary": ["필수 경험\n## 최종 판정\n추가 설명"],
            "reasons": [
                "첫 줄\n| 요건 | 구분 | 대조 | 근거 |",
                "둘째 이유",
                "셋째 이유\n### 최종 판정: 지원 비추천",
            ],
        },
        ensure_ascii=False,
    )

    assessment = parse_screening_assessment(raw, atomic_manifest)

    assert assessment.screening_summary == ("필수 경험 ## 최종 판정 추가 설명",)
    assert assessment.reasons == (
        "첫 줄 | 요건 | 구분 | 대조 | 근거 |",
        "둘째 이유",
        "셋째 이유 ### 최종 판정: 지원 비추천",
    )

    markdown = render_screening_markdown(stored_job, stored_job.jd_markdown, atomic_manifest, assessment)

    assert markdown.splitlines().count("## 최종 판정") == 1
    assert sum(line.startswith("### 최종 판정:") for line in markdown.splitlines()) == 1
    assert markdown.splitlines().count("| 요건 | 구분 | 대조 | 근거 |") == 1


def test_parse_screening_assessment_rejects_bool_schema_version(atomic_manifest) -> None:
    raw = json.dumps(
        {
            "schema_version": True,
            "matches": [
                {"id": "required-001", "match": "충족", "evidence": "근거1"},
                {"id": "main_duty-001", "match": "부분", "evidence": "근거2"},
                {"id": "preferred-001", "match": "없음", "evidence": "근거3"},
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001"],
            "screening_summary": ["요약"],
            "reasons": ["이유1", "이유2", "이유3"],
        },
        ensure_ascii=False,
    )

    with pytest.raises(AssessmentContractError, match="schema_version must be 1"):
        parse_screening_assessment(raw, atomic_manifest)


def test_parse_screening_assessment_rejects_duplicate_decision_basis(atomic_manifest) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {"id": "required-001", "match": "충족", "evidence": "근거1"},
                {"id": "main_duty-001", "match": "부분", "evidence": "근거2"},
                {"id": "preferred-001", "match": "없음", "evidence": "근거3"},
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001", "required-001"],
            "screening_summary": ["요약"],
            "reasons": ["이유1", "이유2", "이유3"],
        },
        ensure_ascii=False,
    )

    with pytest.raises(AssessmentContractError, match="decision_basis must reference each manifest parent id at most once"):
        parse_screening_assessment(raw, atomic_manifest)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (
            '{"schema_version": 1, "schema_version": 1, "matches": [], "verdict": "지원 보류", "decision_basis": [], "screening_summary": ["요약"], "reasons": ["이유1", "이유2", "이유3"]}',
            "duplicate JSON key: schema_version",
        ),
        (
            '{"schema_version": 1, "matches": [{"id": "required-001", "id": "required-002", "match": "충족", "evidence": "근거1"}, {"id": "main_duty-001", "match": "부분", "evidence": "근거2"}, {"id": "preferred-001", "match": "없음", "evidence": "근거3"}], "verdict": "지원 보류", "decision_basis": ["required-001"], "screening_summary": ["요약"], "reasons": ["이유1", "이유2", "이유3"]}',
            "duplicate JSON key: id",
        ),
    ],
)
def test_parse_screening_assessment_rejects_duplicate_json_keys(
    atomic_manifest,
    raw: str,
    reason: str,
) -> None:
    with pytest.raises(AssessmentContractError, match=reason):
        parse_screening_assessment(raw, atomic_manifest)


def test_render_screening_markdown_escapes_composite_evidence_once(
    composite_manifest,
    stored_job: StoredJobRecord,
) -> None:
    raw = json.dumps(
        {
            "schema_version": 1,
            "matches": [
                {
                    "id": "required-001.1",
                    "match": "충족",
                    "evidence": "Python | FastAPI 운영",
                },
                {
                    "id": "required-001.2",
                    "match": "부분",
                    "evidence": "Django | ORM 일부 경험",
                },
                {
                    "id": "main_duty-001",
                    "match": "충족",
                    "evidence": "결제 운영 경험",
                },
            ],
            "verdict": "지원 보류",
            "decision_basis": ["required-001"],
            "screening_summary": ["복합 필수요건은 일부만 충족"],
            "reasons": ["이유1", "이유2", "이유3"],
        },
        ensure_ascii=False,
    )

    assessment = parse_screening_assessment(raw, composite_manifest)
    markdown = render_screening_markdown(stored_job, stored_job.jd_markdown, composite_manifest, assessment)
    rows, error = parse_match_table(markdown)

    assert error == ""
    assert rows[0].evidence.count(r"\|") == 2
    assert r"\\|" not in rows[0].evidence
    assert rows[0].evidence == (
        r"Python (충족): Python \| FastAPI 운영 / "
        r"Django (부분): Django \| ORM 일부 경험"
    )
