from __future__ import annotations

import json
from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest, without_main_duty
from careerkit.jobs.application.screening import (
    build_company_risk_summary,
    build_fallback_output,
    build_prompt,
    is_fallback_document,
    load_screening_rules,
    rewrite_verdict_line,
    run_screening,
    validate_screening_structure,
)
from careerkit.jobs.domain.model import JobKey, JobRecord, ScreeningVerdict
from careerkit.jobs.domain.verdict import parse_verdict_candidates
from careerkit.workspace import resolve_workspace


BASE_JD = """# Gate Role

## 자격 요건
- Spring Boot 백엔드 개발 경험 필수
- Kafka 운영 경험 필수

## 주요 업무
- 결제 서비스 운영

## 우대 사항
- AWS 운영 경험
"""

AMBIGUOUS_JD = """# Ambiguous Role

## 자격 요건
협업 태도와 문제 해결 의지를 중요하게 본다.

## 주요 업무
- 데이터 파이프라인 운영
"""


class SequenceProvider:
    def __init__(self, outputs: list[str], provider_name: str = "fake-codex") -> None:
        self.outputs = outputs
        self.provider_name = provider_name
        self.calls = 0
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        self.prompts.append(prompt)
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return self.provider_name, output


class FailingProvider:
    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        raise RuntimeError("SENTINEL_SECRET_TOKEN failure while contacting upstream")


def _assessment_json(
    manifest,
    *,
    match_overrides: dict[str, str] | None = None,
    evidence_overrides: dict[str, str] | None = None,
    verdict: str = "지원 추천",
    decision_basis: list[str] | None = None,
    summary: list[str] | None = None,
    reasons: list[str] | None = None,
) -> str:
    matches = []
    match_overrides = match_overrides or {}
    evidence_overrides = evidence_overrides or {}
    for item in manifest.leaves:
        if not item.assessable:
            continue
        matches.append(
            {
                "id": item.id,
                "match": match_overrides.get(item.id, "충족"),
                "evidence": evidence_overrides.get(
                    item.id,
                    f"[source: private/profile/skills-job.md] {item.text} 근거",
                ),
            }
        )
    payload = {
        "schema_version": 1,
        "matches": matches,
        "verdict": verdict,
        "decision_basis": decision_basis or [],
        "screening_summary": summary or ["요건 기반 구조화 평가를 완료했다"],
        "reasons": reasons
        or [
            "후보자 이력의 명시 근거만 사용했다",
            "source-owned requirement manifest를 그대로 따랐다",
            "최종 판정은 구조화된 대조 결과로 작성했다",
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def make_workspace(tmp_path: Path):
    (tmp_path / ".career-workspace").write_text("1", encoding="utf-8")
    (tmp_path / "private/profile").mkdir(parents=True)
    (tmp_path / "private/companies/acme/projects").mkdir(parents=True)
    (tmp_path / "private/jd/config").mkdir(parents=True)
    (tmp_path / "private/profile/summary-job.md").write_text("summary", encoding="utf-8")
    (tmp_path / "private/profile/skills-job.md").write_text("skills", encoding="utf-8")
    (tmp_path / "private/companies/acme/profile.md").write_text("company profile", encoding="utf-8")
    (tmp_path / "private/companies/acme/projects/project-a.md").write_text(
        "project details",
        encoding="utf-8",
    )
    (tmp_path / "private/jd/config/jd-screening-rules.md").write_text(
        "# Rules\n- backend\n- decisive required gap only",
        encoding="utf-8",
    )
    return resolve_workspace(explicit=tmp_path)


def _create_record(tmp_path: Path, *, jd_markdown: str = BASE_JD, job_id: str = "100002"):
    workspace = make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    stored = repository.create(
        JobRecord(
            platform="wanted",
            job_id=job_id,
            company="GateCo",
            position="Backend Engineer",
            source_url=f"https://example.com/jobs/{job_id}",
        ),
        jd_markdown=jd_markdown,
    )
    return workspace, repository, stored


def test_run_screening_publishes_rendered_markdown_and_metadata(tmp_path: Path) -> None:
    """Covers S1."""
    workspace, repository, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest))]),
        repository=repository,
        dry_run=False,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.verdict == "지원 추천"
    assert result.used_fallback is False
    assert persisted.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert persisted.screening_markdown is not None
    assert "주요업무" not in persisted.screening_markdown
    assert "required-001" not in persisted.screening_markdown
    assert result.evidence_violations["unevidenced_main_duty"] == 0


def test_build_prompt_embeds_source_owned_manifest_and_json_contract(tmp_path: Path) -> None:
    workspace, _, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    prompt = build_prompt(
        workspace=workspace,
        jd_content=stored.jd_markdown,
        rules=load_screening_rules(workspace),
        company_content="company body",
        company_risk_summary="risk body",
        candidate_context="candidate body",
        manifest=manifest,
    )

    assert "JSON 객체 하나만 허용" in prompt
    assert '"match_targets"' in prompt
    assert '"id": "required-001"' in prompt
    assert '"kind": "주요업무"' in prompt
    assert "candidate body" in prompt
    assert "company body" in prompt
    assert stored.jd_markdown in prompt


def test_run_screening_does_not_send_private_profile_or_company_files_by_default(tmp_path: Path) -> None:
    workspace, _, stored = _create_record(tmp_path)
    (tmp_path / "private/profile/summary-job.md").write_text("PRIVATE_PROFILE_SENTINEL", encoding="utf-8")
    (tmp_path / "private/companies/acme/profile.md").write_text("PRIVATE_COMPANY_SENTINEL", encoding="utf-8")
    provider = SequenceProvider([_assessment_json(without_main_duty(extract_requirement_manifest(stored.jd_markdown)))])

    run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=True,
        llm_provider=provider,
    )

    assert "PRIVATE_PROFILE_SENTINEL" not in provider.prompts[0]
    assert "PRIVATE_COMPANY_SENTINEL" not in provider.prompts[0]
    assert "후보자 이력/경험 근거는 호출자가 제공하지 않았음" in provider.prompts[0]


def test_invalid_first_response_gets_contract_specific_retry(tmp_path: Path) -> None:
    workspace, _, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    provider = SequenceProvider(["```json\n{}\n```", _assessment_json(without_main_duty(manifest))])

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=True,
        llm_provider=provider,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
    )

    assert provider.calls == 2
    assert result.used_fallback is False
    assert result.verdict == "지원 추천"
    assert "이전 응답이 JSON 계약을 위반했습니다" in provider.prompts[1]
    assert '"id": "required-001"' in provider.prompts[0]
    assert '"id": "required-001"' in provider.prompts[1]


def test_screening_result_constructor_keeps_fallback_reason_optional() -> None:
    result = run_screening.__globals__["ScreeningResult"](
        verdict="지원 보류",
        screening_path=Path("wanted/1/screening.md"),
        provider="fallback",
        used_fallback=True,
        raw_output="raw",
    )

    assert result.fallback_reason is None


def test_invalid_retry_publishes_fallback_hold_without_raw_json(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)
    provider = SequenceProvider(["{}", "{}"])

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=provider,
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.used_fallback is True
    assert result.verdict == "지원 보류"
    assert persisted.record.screening_verdict is ScreeningVerdict.HOLD
    assert persisted.screening_markdown is not None
    assert "schema_version" not in persisted.screening_markdown
    assert "원시 실행 로그를 저장하지 않고" in persisted.screening_markdown


def test_prose_only_qualifications_fail_before_provider_and_repository_mutation(tmp_path: Path) -> None:
    """Covers S3."""
    workspace, repository, stored = _create_record(tmp_path, jd_markdown=AMBIGUOUS_JD)
    provider = SequenceProvider(["{}"])

    with pytest.raises(ValueError, match="screening-no-assessable-requirements"):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=False,
            llm_provider=provider,
            repository=repository,
            candidate_context="[source: private/profile/skills-job.md] 데이터 파이프라인 운영",
        )

    assert provider.calls == 0
    persisted = repository.get(JobKey("wanted", "100002"))
    assert persisted.screening_markdown is None
    assert persisted.record.screening_verdict is None
    assert persisted.record.screening_provider is None


def test_non_required_only_decision_basis_publishes_hold(tmp_path: Path) -> None:
    """Non-필수 decision_basis alone cannot support 비추천 (was S4 for main-duty)."""
    workspace, repository, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    filtered = without_main_duty(manifest)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider(
            [
                _assessment_json(
                    filtered,
                    verdict="지원 비추천",
                    decision_basis=[],
                    summary=["비결정적 근거만으로 비추천을 시도했다"],
                )
            ]
        ),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.verdict == "지원 보류"
    assert result.evidence_violations["unsupported_not_recommended"] == 1
    assert persisted.record.screening_verdict is ScreeningVerdict.HOLD


@pytest.mark.parametrize(
    ("jd_markdown", "provider_output", "expected_reason"),
    [
        (
            BASE_JD,
            lambda manifest: _assessment_json(
                manifest,
                verdict="지원 비추천",
                decision_basis=[],
                summary=["정책 문장만으로 비추천을 시도했다"],
            ),
            "policy-only",
        ),
    ],
)
def test_conservative_guard_forces_hold_and_synchronizes_published_verdict(
    tmp_path: Path,
    jd_markdown: str,
    provider_output,
    expected_reason: str,
) -> None:
    workspace, repository, stored = _create_record(tmp_path, jd_markdown=jd_markdown)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider([provider_output(without_main_duty(manifest))]),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.verdict == "지원 보류", expected_reason
    assert persisted.record.screening_verdict is ScreeningVerdict.HOLD
    assert persisted.screening_markdown is not None
    assert set(parse_verdict_candidates(persisted.screening_markdown)) == {"지원 보류"}
    if expected_reason == "policy-only":
        assert result.evidence_violations["unsupported_not_recommended"] == 1


def test_decisive_missing_parent_allows_not_recommended(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    filtered = without_main_duty(manifest)
    provider = SequenceProvider(
        [
            _assessment_json(
                filtered,
                verdict="지원 비추천",
                match_overrides={
                    "required-001": "없음",
                    "required-002": "충족",
                    "preferred-001": "충족",
                },
                decision_basis=["required-001"],
                summary=["결정적 필수요건 부족이 확인됐다"],
            )
        ]
    )

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=provider,
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Kafka, 결제 운영, AWS",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.verdict == "지원 비추천"
    assert result.evidence_violations.get("unsupported_not_recommended") is None
    assert persisted.record.screening_verdict is ScreeningVerdict.NOT_RECOMMENDED
    assert persisted.screening_markdown is not None
    assert "### 최종 판정: 지원 비추천" in persisted.screening_markdown


def test_duplicate_parent_text_does_not_borrow_missing_match_for_not_recommended(
    tmp_path: Path,
) -> None:
    jd_markdown = """# Duplicate Role

## 자격 요건
- Python 경험 필수
- Python 경험 필수
"""
    workspace, repository, stored = _create_record(tmp_path, jd_markdown=jd_markdown)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    filtered = without_main_duty(manifest)
    provider = SequenceProvider(
        [
            _assessment_json(
                filtered,
                verdict="지원 비추천",
                match_overrides={
                    "required-001": "충족",
                    "required-002": "없음",
                },
                decision_basis=["required-001"],
            )
        ]
    )

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=provider,
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Python 경험",
    )

    assert result.verdict == "지원 보류"
    assert result.evidence_violations["unsupported_not_recommended"] == 1


def test_unknown_requirement_ids_cannot_publish(tmp_path: Path) -> None:
    """Covers S2."""
    workspace, repository, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    payload = json.loads(_assessment_json(without_main_duty(manifest)))
    payload["matches"][0]["id"] = "required-999"
    provider = SequenceProvider([json.dumps(payload, ensure_ascii=False), json.dumps(payload, ensure_ascii=False)])

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=provider,
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.used_fallback is True
    assert persisted.screening_markdown is not None
    assert "required-999" not in persisted.screening_markdown
    assert "지원 보류" in persisted.screening_markdown


def test_local_provider_recommendation_is_capped_after_structured_rendering(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest))], provider_name="ollama"),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.verdict == "지원 보류"
    assert result.verdict_capped is True
    assert persisted.record.screening_verdict is ScreeningVerdict.HOLD
    assert persisted.record.verdict_capped is True
    assert persisted.screening_markdown is not None
    assert "지원 추천" not in persisted.screening_markdown


@pytest.mark.parametrize(
    ("match_overrides", "expected_verdict"),
    [
        ({}, "지원 추천"),
        ({"required-001": "없음"}, "지원 추천"),
        ({"required-001": "없음", "required-002": "없음"}, "지원 보류"),
        (
            {
                "required-001": "없음",
                "preferred-001": "없음",
            },
            "지원 추천",
        ),
    ],
)
def test_required_missing_threshold_counts_required_parents_only(
    tmp_path: Path,
    match_overrides: dict[str, str],
    expected_verdict: str,
) -> None:
    workspace, _, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=True,
        llm_provider=SequenceProvider(
            [_assessment_json(without_main_duty(manifest), match_overrides=match_overrides)],
            provider_name="codex",
        ),
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, AWS",
    )

    assert result.verdict == expected_verdict


def test_company_risk_summary_uses_computed_validator_flags(tmp_path: Path) -> None:
    company_file = tmp_path / "company.md"
    company_file.write_text(
        "# Risky Co\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2021년 |\n| 직원수 | 100명 |\n| 업종 | IT |\n\n"
        "## 인원 통계\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 1년간 입사자 | 30명 |\n| 1년간 퇴사자 | 60명 |\n\n"
        "## 연봉 정보\n\n"
        "| 항목 | 금액 |\n|------|------|\n| 평균 연봉 | **4,800만원** |\n",
        encoding="utf-8",
    )

    summary = build_company_risk_summary(company_file)
    assert "TURNOVER_CRITICAL" in summary
    assert "완성도" in summary


def test_main_duty_only_manifest_fails_before_provider_and_repository_mutation(tmp_path: Path) -> None:
    jd_markdown = """# Main Duty Only Role

## 주요 업무
- 결제 서비스 운영
"""
    workspace, repository, stored = _create_record(tmp_path, jd_markdown=jd_markdown)
    provider = SequenceProvider(["{}"])

    with pytest.raises(ValueError, match="screening-no-assessable-requirements"):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=False,
            llm_provider=provider,
            repository=repository,
            candidate_context="[source: private/profile/skills-job.md] 결제 서비스 운영",
        )

    assert provider.calls == 0
    assert repository.get(JobKey("wanted", "100002")).screening_markdown is None


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (lambda doc: doc.replace("| 생성 방식 | 자동 fallback |", "| 생성 방식 | 수동 fallback |"), "구조 검증 실패"),
        (lambda doc: doc.replace("## 스크리닝 결과\n\nLLM", "## 핵심 근거\n\n- 먼저 씀\n\n## 스크리닝 결과\n\nLLM", 1), "구조 검증 실패"),
        (lambda doc: doc.replace("| 항목 | 판단 |", "| 항목 | 근거 |"), "구조 검증 실패"),
        (lambda doc: doc.replace("| JD 필수요건 대조 | 수동 재스크리닝 필요. |", "| JD 필수요건 대조 | 수동 재스크리닝 필요. |\n| 추가 행 | 위조 |"), "구조 검증 실패"),
        (lambda doc: doc.replace("| 항목 | 판단 |\n|------|------|", "| 요건 | 구분 | 대조 | 근거 |\n|------|------|------|------|", 1), "구조 검증 실패"),
        (lambda doc: doc.replace("### 최종 판정: 지원 보류", "### 최종 판정: 지원 추천"), "구조 검증 실패"),
    ],
)
def test_forged_fallback_document_is_rejected_before_publication(tmp_path: Path, monkeypatch, mutate, expected_reason: str) -> None:
    from careerkit.jobs.application import screening as screening_module

    workspace, repository, stored = _create_record(tmp_path)
    original = screening_module.build_fallback_output

    def forged(*args, **kwargs):
        return mutate(original(*args, **kwargs))

    monkeypatch.setattr(screening_module, "build_fallback_output", forged)

    with pytest.raises(RuntimeError, match=expected_reason):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=False,
            llm_provider=SequenceProvider(["{}", "{}"]),
            repository=repository,
            candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
        )

    assert repository.get(JobKey("wanted", "100002")).screening_markdown is None
    assert repository.get_metadata(JobKey("wanted", "100002")).record.screening_provider is None


def test_two_invalid_assessments_publish_fallback_with_contract_exhausted_reason(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider(["{}", "{}"], provider_name="codex"),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert result.provider == "fallback"
    assert result.fallback_reason == "assessment-contract-exhausted"
    assert persisted.record.screening_provider == "fallback"


def test_invalid_assessment_then_provider_exhaustion_publish_fallback_with_provider_exhausted_reason(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)

    class RetryFailProvider:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, prompt: str, timeout: int, local_timeout: int | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                return "codex", "{}"
            raise RuntimeError("second attempt failed")

    provider = RetryFailProvider()
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=provider,
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka",
    )

    persisted = repository.get(JobKey("wanted", "100002"))
    assert provider.calls == 2
    assert result.provider == "fallback"
    assert result.fallback_reason == "provider-exhausted"
    assert persisted.record.screening_provider == "fallback"


def test_fallback_dry_run_reports_run_local_uncapped_state(tmp_path: Path) -> None:
    workspace, repository, _ = _publish_initial_screening(tmp_path, provider_name="ollama", job_id="371154")
    key = JobKey("wanted", "371154")
    before = repository.get(key).screening_markdown

    result = run_screening(
        workspace=workspace,
        jd=repository.get(key),
        company_file=None,
        dry_run=True,
        llm_provider=FailingProvider(),
        candidate_context="[source: private/profile/skills-job.md] Spring Boot",
    )

    assert result.used_fallback is True
    assert result.verdict_capped is False
    assert repository.get_metadata(key).record.verdict_capped is True
    assert repository.get(key).screening_markdown == before


def test_screening_fallback_redacts_error_into_safe_hold_document(tmp_path: Path) -> None:
    workspace, repository, stored = _create_record(tmp_path)
    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=FailingProvider(),
        repository=repository,
        candidate_context="sanitized candidate context",
    )
    persisted = repository.get(JobKey("wanted", "100002"))
    assert persisted.screening_markdown is not None
    assert result.used_fallback is True
    assert result.verdict == "지원 보류"
    assert "SENTINEL_SECRET_TOKEN" not in result.raw_output
    assert "[redacted]" in result.raw_output
    assert "지원 보류" in persisted.screening_markdown
    assert "원시 실행 로그를 저장하지 않고" in persisted.screening_markdown


def test_validate_screening_structure_rejects_conversational_output() -> None:
    valid, reason = validate_screening_structure(
        "## 기본 정보\n\n해드리겠습니다\n\n## 스크리닝 결과\n\nA\n\n## 이력/경험 매칭\n\nB\n\n## 최종 판정\n\n### 최종 판정: 지원 보류\n\n## 핵심 근거\n\nC\n"
    )
    assert valid is False
    assert "대화형 패턴" in reason


def test_structure_validation_accepts_the_fallback_document() -> None:
    document = build_fallback_output(
        stored := repository_record_stub(),
        stored.jd_markdown,
        "provider unavailable",
    )
    assert validate_screening_structure(document) == (True, "")


@pytest.mark.parametrize(
    ("jd_markdown", "reason"),
    [
        ("# JD\n", "command A | command B"),
        (
            "---\ncompany: Company A | Company B\nposition: Platform | Backend\n---\n출처: [link](https://example.com/a|b)\n# JD\n",
            "command A | command B",
        ),
    ],
)
def test_build_fallback_output_with_dynamic_pipe_cells_round_trips_cleanly(
    tmp_path: Path,
    jd_markdown: str,
    reason: str,
) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    stored = repository.create(
        JobRecord(
            platform="wanted",
            job_id="77",
            company="Example",
            position="Backend Engineer",
            screening_verdict=ScreeningVerdict.HOLD,
        ),
        jd_markdown=jd_markdown,
    )
    document = build_fallback_output(stored, stored.jd_markdown, reason)
    repository.update_screening_result(
        stored.record.key,
        screening_markdown=document,
        screening_provider="fallback",
    )

    assert is_fallback_document(document) is True
    assert validate_screening_structure(document) == (True, "")

    findings = __import__("careerkit.jobs.application.screening_lint", fromlist=["screening_lint"]).lint_record(stored.record.key, repository)
    assert findings == []


def test_build_fallback_output_is_recognised_by_is_fallback_document() -> None:
    """Round-trip: the writer and the selector must agree."""
    from careerkit.jobs.adapters.storage.file_records import StoredJobRecord

    document = build_fallback_output(
        StoredJobRecord(
            record=JobRecord("wanted", "99", "TestCo", "Engineer"),
            jd_markdown="# JD\n\nSome content",
            screening_markdown=None,
        ),
        "# JD\n\nSome content",
        "timeout after 120s",
    )

    assert is_fallback_document(document) is True


def test_fallback_document_with_conversational_reason_still_validates_and_publishes(
    tmp_path: Path,
) -> None:
    _, repository, stored = _create_record(tmp_path)
    document = build_fallback_output(stored, stored.jd_markdown, "승인 대기 중 upstream timeout")

    assert validate_screening_structure(document) == (True, "")
    assert is_fallback_document(document) is True

    repository.update_screening_result(
        stored.record.key,
        screening_markdown=document,
        screening_provider="fallback",
    )

    persisted = repository.get(stored.record.key)
    assert persisted.screening_markdown == document


def test_conversational_pattern_still_rejects_normal_provider_output() -> None:
    document = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | RealCo |
| 포지션 | Backend |

## 스크리닝 결과

승인 대기 중

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|------|------|------|------|
| Python 3년 | 필수 | 충족 | 이력서 명시 |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 요건 충족.
"""

    assert validate_screening_structure(document) == (False, "대화형 패턴 탐지: '승인 대기 중'")


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda doc: doc.replace("| 회사명 | 확인 필요 |", "| 회사명 |  |"), "blank dynamic cell"),
        (lambda doc: doc.replace("| 회사명 | 확인 필요 |", "| 회사명 | 확인 | 필요 |"), "extra basic-info column"),
        (lambda doc: doc.replace("- 실패 사유: timeout after 120s", "- 실패 사유: timeout | after 120s"), "unescaped separator"),
    ],
)
def test_is_fallback_document_rejects_forged_dynamic_cells(mutate, label: str) -> None:
    document = build_fallback_output(
        repository_record_stub(),
        repository_record_stub().jd_markdown,
        "timeout after 120s",
    )

    assert is_fallback_document(mutate(document)) is False, label



def test_normal_four_column_screening_with_generic_generation_phrase_remains_valid() -> None:
    document = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | RealCo |
| 포지션 | Backend |

## 스크리닝 결과

- 생성 방식 설명은 일반 본문이다

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|------|------|------|------|
| Python 3년 | 필수 | 충족 | 이력서 명시 |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 요건 충족.
"""

    assert validate_screening_structure(document) == (True, "")


def test_is_fallback_document_rejects_real_screening() -> None:
    real = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | RealCo |

## 스크리닝 결과

실질 분석 수행됨.

## 이력/경험 매칭

| 항목 | 충족 여부 | 근거 | 출처 |
|------|-----------|------|------|
| Python 3년 | ⭕ 충족 | 이력서 명시 | [resume] |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 요건 충족.
"""
    assert is_fallback_document(real) is False


def test_a_marker_row_alone_does_not_buy_the_fallback_exemption() -> None:
    """The text being matched is model output, so one recognisable line proves
    nothing — a provider that emitted it would otherwise skip the table check."""
    forged = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | GateCo |
| 생성 방식 | 자동 fallback |

## 스크리닝 결과

요건과 이력을 대조했다.

## 이력/경험 매칭

없음

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 첫 번째 근거 문장이다.
- 두 번째 근거 문장이다.
"""

    assert validate_screening_structure(forged) == (False, "fallback 문서 계약 위반")


def repository_record_stub():
    from careerkit.jobs.adapters.storage.file_records import StoredJobRecord

    return StoredJobRecord(
        record=JobRecord("wanted", "1", "Acme", "Backend"),
        jd_markdown="# JD",
        screening_markdown=None,
    )


def test_rewrite_verdict_line_requires_a_verdict_line() -> None:
    with pytest.raises(ValueError):
        rewrite_verdict_line("## 기본 정보\n\n본문\n", "지원 보류")


def _publish_initial_screening(
    tmp_path: Path,
    *,
    provider_name: str = "ollama",
    job_id: str = "100002",
):
    workspace, repository, stored = _create_record(tmp_path, job_id=job_id)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest))], provider_name=provider_name),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
    )
    return workspace, repository, repository.get(JobKey("wanted", job_id))


def test_local_provider_publication_is_withheld_when_a_strong_provider_is_required(
    tmp_path: Path,
) -> None:
    workspace, repository, _ = _publish_initial_screening(tmp_path, provider_name="ollama", job_id="100003")
    original = repository.get(JobKey("wanted", "100003")).screening_markdown

    manifest = extract_requirement_manifest(repository.get(JobKey("wanted", "100003")).jd_markdown)
    result = run_screening(
        workspace=workspace,
        jd=repository.get(JobKey("wanted", "100003")),
        company_file=None,
        dry_run=False,
        llm_provider=SequenceProvider(
            [_assessment_json(without_main_duty(manifest), reasons=["덮어쓰기 시도", "근거 2", "근거 3"])],
            provider_name="ollama",
        ),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
        require_strong_provider=True,
    )

    assert result.published is False
    assert repository.get(JobKey("wanted", "100003")).screening_markdown == original


def test_ambiguous_manifest_with_one_assessable_leaf_preserves_hold_without_cap(tmp_path: Path) -> None:
    jd_markdown = """# Mixed Ambiguous Role

## 자격 요건
협업 태도와 문제 해결 의지를 중요하게 본다.
- 데이터 파이프라인 운영
"""
    workspace, _, stored = _create_record(tmp_path, jd_markdown=jd_markdown)
    manifest = extract_requirement_manifest(stored.jd_markdown)

    result = run_screening(
        workspace=workspace,
        jd=stored,
        company_file=None,
        dry_run=True,
        llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest), verdict="지원 추천")], provider_name="codex"),
        candidate_context="[source: private/profile/skills-job.md] 데이터 파이프라인 운영",
    )

    assert result.verdict == "지원 보류"
    assert result.verdict_capped is False


def test_fallback_publication_preserves_an_existing_cap(tmp_path: Path) -> None:
    workspace, repository, _ = _publish_initial_screening(tmp_path, provider_name="ollama", job_id="100004")

    result = run_screening(
        workspace=workspace,
        jd=repository.get(JobKey("wanted", "100004")),
        company_file=None,
        dry_run=False,
        llm_provider=FailingProvider(),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot",
    )

    assert result.used_fallback is True
    assert result.verdict_capped is True
    assert repository.get_metadata(JobKey("wanted", "100004")).record.verdict_capped is True


def test_fallback_publication_is_withheld_when_a_strong_provider_is_required(
    tmp_path: Path,
) -> None:
    workspace, repository, _ = _publish_initial_screening(tmp_path, provider_name="ollama", job_id="100005")
    key = JobKey("wanted", "100005")
    original = repository.get(key).screening_markdown

    result = run_screening(
        workspace=workspace,
        jd=repository.get(key),
        company_file=None,
        dry_run=False,
        llm_provider=FailingProvider(),
        repository=repository,
        candidate_context="[source: private/profile/skills-job.md] Spring Boot",
        require_strong_provider=True,
    )

    assert result.used_fallback is True
    assert result.published is False
    assert repository.get(key).screening_markdown == original
    assert repository.get_metadata(key).record.verdict_capped is True


def test_a_residual_better_verdict_outside_the_published_verdict_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    from careerkit.jobs.application import screening as screening_module

    original_rewrite = screening_module.rewrite_verdict_line

    def append_residual(markdown: str, verdict: str) -> str:
        rewritten = original_rewrite(markdown, verdict)
        return rewritten.replace("## 핵심 근거", "#### 지원 추천\n\n## 핵심 근거", 1)

    monkeypatch.setattr(screening_module, "rewrite_verdict_line", append_residual)
    workspace, _, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)

    with pytest.raises(RuntimeError, match="상위 판정 잔존"):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=True,
            llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest))], provider_name="ollama"),
            candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
        )


def test_gate_fails_closed_when_demotion_breaks_the_table(
    tmp_path: Path, monkeypatch
) -> None:
    from careerkit.jobs.application import screening as screening_module

    workspace, _, stored = _create_record(tmp_path)
    manifest = extract_requirement_manifest(stored.jd_markdown)
    monkeypatch.setattr(
        screening_module,
        "apply_demotions",
        lambda markdown, rows, demoted: markdown.replace("| 필수 |", "| 담당 |"),
    )

    with pytest.raises(RuntimeError, match="강등 적용 후 표 파싱 실패"):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=True,
            llm_provider=SequenceProvider(
                [
                    _assessment_json(
                        without_main_duty(manifest),
                        evidence_overrides={
                            "required-001": "있다고 주장",
                            "required-002": "있다고 주장",
                        },
                    )
                ],
                provider_name="codex",
            ),
            candidate_context="[source: private/profile/skills-job.md] Spring Boot",
        )


def test_renderer_exception_does_not_replace_an_existing_screening(tmp_path: Path, monkeypatch) -> None:
    from careerkit.jobs.application import screening as screening_module

    workspace, repository, stored = _publish_initial_screening(
        tmp_path,
        provider_name="codex",
        job_id="100006",
    )
    key = JobKey("wanted", "100006")
    original = repository.get(key).screening_markdown
    original_provider = repository.get_metadata(key).record.screening_provider
    manifest = extract_requirement_manifest(stored.jd_markdown)

    monkeypatch.setattr(
        screening_module,
        "render_screening_markdown",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("renderer sentinel")),
    )

    with pytest.raises(RuntimeError, match="renderer sentinel"):
        run_screening(
            workspace=workspace,
            jd=stored,
            company_file=None,
            dry_run=False,
            llm_provider=SequenceProvider([_assessment_json(without_main_duty(manifest))], provider_name="codex"),
            repository=repository,
            candidate_context="[source: private/profile/skills-job.md] Spring Boot, Kafka, 결제 운영, AWS",
        )

    assert repository.get(key).screening_markdown == original
    assert repository.get_metadata(key).record.screening_provider == original_provider
