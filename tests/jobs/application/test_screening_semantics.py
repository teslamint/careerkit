from __future__ import annotations

import json

import pytest

from careerkit.jobs.application import screening_semantics
from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest
from careerkit.jobs.application.screening_assessment import parse_screening_assessment
from careerkit.jobs.application.screening_semantics import (
    CalibratedSemanticValidator,
    SemanticJudgeCalibration,
    SemanticJudgeError,
    build_semantic_claims,
    evaluate_semantic_judge,
    load_semantic_eval_cases,
    validate_semantic_assessment,
)


class SequenceJudge:
    def __init__(self, outputs: list[str], provider: str = "codex") -> None:
        self.outputs = outputs
        self.provider = provider
        self.calls = 0

    def run(self, prompt: str, timeout: int, local_timeout: int | None = None) -> tuple[str, str]:
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return self.provider, output


def _judgments(labels: dict[str, str], spans: dict[str, list[str]]) -> str:
    return json.dumps({
        "schema_version": 1,
        "judgments": [{
            "id": item_id,
            "label": label,
            "evidence_spans": spans[item_id],
            "reason": "요건 전체와 인용 근거의 의미 범위를 비교했다.",
        } for item_id, label in labels.items()],
    }, ensure_ascii=False)


def _single_claim_assessment(match: str, evidence: str):
    manifest = extract_requirement_manifest("## 자격요건\n- Spring Boot 개발 경험\n")
    assessment = parse_screening_assessment(json.dumps({
        "schema_version": 1,
        "matches": [{"id": "required-001", "match": match, "evidence": evidence}],
        "verdict": "지원 보류",
        "decision_basis": [],
        "screening_summary": [f"필수 1항목: {match} 1"],
        "reasons": ["근거 확인", "요건 확인", "판정 완료"],
    }, ensure_ascii=False), manifest)
    return manifest, assessment


def test_semantic_claims_preserve_unsplit_parent_meaning() -> None:
    manifest = extract_requirement_manifest(
        "## 자격요건\n- 백엔드 서비스, 분산 시스템 또는 플랫폼 개발 경험 7년 이상\n"
    )
    assessment = parse_screening_assessment(json.dumps({
        "schema_version": 1,
        "matches": [
            {"id":"required-001.1","match":"충족","evidence":"probable [source: profile.md] [quote: 백엔드 개발 8년]"},
            {"id":"required-001.2","match":"부분","evidence":"plausible [source: profile.md] [quote: 플랫폼 운영]"},
        ],
        "verdict":"지원 보류","decision_basis":[],
        "screening_summary":["필수 2항목: 충족 1, 부분 1, 없음 0"],
        "reasons":["추천 전환 조건: [requirement: required-001] 충족 확인","비추천 확정 조건: [requirement: required-001] 미충족 확정","검토 필요"],
    }, ensure_ascii=False), manifest)

    claims = build_semantic_claims(manifest, assessment)

    assert len(claims) == 1
    assert claims[0].requirement == "백엔드 서비스, 분산 시스템 또는 플랫폼 개발 경험 7년 이상"
    assert claims[0].declared_match == "부분"
    assert claims[0].evidence_spans == ("백엔드 개발 8년", "플랫폼 운영")


def test_semantic_eval_dataset_and_thresholds() -> None:
    cases = load_semantic_eval_cases()
    assert len(cases) == 21
    assert sum(case.group == "parent-control" for case in cases) == 14
    assert sum(case.group == "known-overclaim" for case in cases) == 7
    assert {case.expected_label for case in cases} == {"entails", "partial", "unsupported", "contradicted"}
    assert {
        label: sum(case.expected_label == label for case in cases)
        for label in ("entails", "partial", "unsupported", "contradicted")
    } == {"entails": 6, "partial": 6, "unsupported": 8, "contradicted": 1}
    assert sum(case.decision_driving for case in cases if case.expected_label in {"unsupported", "contradicted"}) == 2
    labels = {case.id: case.expected_label for case in cases}
    spans = {case.id: [case.evidence_spans[0]] for case in cases}
    report = evaluate_semantic_judge(
        SequenceJudge([_judgments(labels, spans)] * 3),
        timeout=30, runs=3, batch_size=21,
    )
    assert report.passed is True
    assert report.adverse_recall == report.supported_acceptance == report.macro_f1 == 1.0
    with pytest.raises(ValueError, match="semantic-eval-runs"):
        evaluate_semantic_judge(SequenceJudge([]), timeout=30, runs=2)


def test_semantic_eval_loader_rejects_empty_dataset(tmp_path, monkeypatch) -> None:
    eval_file = tmp_path / "screening_semantics.json"
    eval_file.write_text('{"schema_version":1,"cases":[]}', encoding="utf-8")
    monkeypatch.setattr(screening_semantics.resources, "files", lambda _package: tmp_path)

    with pytest.raises(SemanticJudgeError, match="semantic-eval-dataset-contract"):
        load_semantic_eval_cases()


def test_semantic_eval_loader_rejects_non_boolean_decision_flag(tmp_path, monkeypatch) -> None:
    package = screening_semantics.resources.files("careerkit.jobs.resources.evals")
    raw = package.joinpath("screening_semantics.json").read_text(encoding="utf-8")
    (tmp_path / "screening_semantics.json").write_text(
        raw.replace('"decision_driving":true', '"decision_driving":1', 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(screening_semantics.resources, "files", lambda _package: tmp_path)

    with pytest.raises(SemanticJudgeError, match="semantic-eval-dataset-contract"):
        load_semantic_eval_cases()


def test_semantic_validation_rejects_same_provider_and_scope_overclaim() -> None:
    manifest = extract_requirement_manifest("## 우대사항\n- 온프레미스 또는 폐쇄망 배포 경험\n")
    assessment = parse_screening_assessment(json.dumps({
        "schema_version":1,
        "matches":[{"id":"preferred-001","match":"충족","evidence":"probable [source: profile.md] [quote: Ansible 자동화]"}],
        "verdict":"지원 추천","decision_basis":[],
        "screening_summary":["우대 1항목: 충족 1, 부분 0, 없음 0"],
        "reasons":["근거 검토","요건 검토","판정 완료"],
    }, ensure_ascii=False), manifest)
    output = _judgments({"preferred-001":"unsupported"}, {"preferred-001":["Ansible 자동화"]})
    with pytest.raises(SemanticJudgeError, match="independent-provider-required"):
        validate_semantic_assessment(
            manifest, assessment, screening_provider="codex",
            judge=SequenceJudge([output], provider="codex"), timeout=30,
        )
    with pytest.raises(SemanticJudgeError, match="semantic-mismatch:preferred-001"):
        validate_semantic_assessment(
            manifest, assessment, screening_provider="local",
            judge=SequenceJudge([output], provider="codex"), timeout=30,
        )


def test_failed_calibration_blocks_runtime_judge_call() -> None:
    judge = SequenceJudge(["unused"])
    validator = CalibratedSemanticValidator(judge, report=SemanticJudgeCalibration(
        provider="codex", runs=3, adverse_recall=1.0,
        supported_acceptance=0.90, macro_f1=0.89, passed=False,
    ))
    with pytest.raises(SemanticJudgeError, match="semantic-eval-threshold-not-met"):
        validator.validate(None, None, screening_provider="local")  # type: ignore[arg-type]
    assert judge.calls == 0


def test_calibrated_validator_rejects_runtime_provider_drift() -> None:
    manifest, assessment = _single_claim_assessment(
        "충족", "probable [source: profile.md] [quote: Spring Boot 개발]"
    )
    output = _judgments({"required-001": "entails"}, {"required-001": ["Spring Boot 개발"]})
    validator = CalibratedSemanticValidator(
        SequenceJudge([output], provider="claude"),
        report=SemanticJudgeCalibration(
            provider="codex", runs=3, adverse_recall=1.0,
            supported_acceptance=1.0, macro_f1=1.0, passed=True,
        ),
    )

    with pytest.raises(SemanticJudgeError, match="semantic-judge-provider-drift"):
        validator.validate(manifest, assessment, screening_provider="local")


def test_semantic_validation_accepts_empty_spans_when_claim_has_no_evidence() -> None:
    manifest, assessment = _single_claim_assessment("없음", "possible: 직접 근거 없음")
    output = _judgments({"required-001": "unsupported"}, {"required-001": []})

    judgments = validate_semantic_assessment(
        manifest, assessment, screening_provider="local",
        judge=SequenceJudge([output]), timeout=30,
    )

    assert judgments[0].evidence_spans == ()


def test_semantic_validation_requires_available_evidence_span() -> None:
    manifest, assessment = _single_claim_assessment(
        "충족", "probable [source: profile.md] [quote: Spring Boot 개발]"
    )
    output = _judgments({"required-001": "entails"}, {"required-001": []})

    with pytest.raises(SemanticJudgeError, match="evidence-span-required"):
        validate_semantic_assessment(
            manifest, assessment, screening_provider="local",
            judge=SequenceJudge([output]), timeout=30,
        )


def test_semantic_validation_joins_judgments_by_id_not_response_order() -> None:
    manifest = extract_requirement_manifest(
        "## 자격요건\n- Spring Boot 개발 경험\n- 폐쇄망 배포 경험\n"
    )
    assessment = parse_screening_assessment(json.dumps({
        "schema_version":1,
        "matches":[
            {"id":"required-001","match":"충족","evidence":"probable [source: profile.md] [quote: Spring Boot 개발]"},
            {"id":"required-002","match":"없음","evidence":"possible: 직접 근거 없음 [source: profile.md] [quote: 일반 SaaS 배포]"},
        ],
        "verdict":"지원 보류","decision_basis":[],
        "screening_summary":["필수 2항목: 충족 1, 부분 0, 없음 1"],
        "reasons":["추천 전환 조건: [requirement: required-002] 충족 확인","비추천 확정 조건: [requirement: required-002] 미충족 확정","검토 필요"],
    }, ensure_ascii=False), manifest)
    output = json.dumps({"schema_version":1,"judgments":[
        {"id":"required-002","label":"unsupported","evidence_spans":["일반 SaaS 배포"],"reason":"환경 근거가 없다."},
        {"id":"required-001","label":"entails","evidence_spans":["Spring Boot 개발"],"reason":"개발 경험이 직접 일치한다."},
    ]}, ensure_ascii=False)

    judgments = validate_semantic_assessment(
        manifest, assessment, screening_provider="local",
        judge=SequenceJudge([output], provider="codex"), timeout=30,
    )
    assert {item.id for item in judgments} == {"required-001", "required-002"}
