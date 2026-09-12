"""Independent semantic validation for screening requirement claims."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.resources as resources
import json
import re
from typing import Protocol

from careerkit.jobs.application.requirement_manifest import RequirementManifest, aggregate_parent_matches
from careerkit.jobs.application.screening_assessment import ScreeningAssessment

_EVAL_PACKAGE = "careerkit.jobs.resources.evals"
_EVAL_FILE = "screening_semantics.json"
_CITATION = re.compile(r"\[source:\s*[^\]]+\]\s*\[quote:\s*([^\]]+)\]")
_LABELS = ("entails", "partial", "unsupported", "contradicted")
_ADVERSE = frozenset({"unsupported", "contradicted"})
_SUPPORTED = frozenset({"entails", "partial"})


class SemanticJudge(Protocol):
    def run(self, prompt: str, timeout: int, local_timeout: int | None = None) -> tuple[str, str]: ...


class SemanticJudgeError(ValueError):
    """The semantic result is unsafe or does not satisfy its contract."""


@dataclass(frozen=True)
class SemanticClaim:
    id: str
    requirement: str
    declared_match: str
    evidence_spans: tuple[str, ...]
    kind: str
    decisive: bool


@dataclass(frozen=True)
class SemanticEvalCase:
    id: str
    group: str
    requirement: str
    declared_match: str
    evidence_spans: tuple[str, ...]
    expected_label: str
    decision_driving: bool


@dataclass(frozen=True)
class SemanticJudgment:
    id: str
    label: str
    evidence_spans: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SemanticJudgeCalibration:
    provider: str
    runs: int
    adverse_recall: float
    supported_acceptance: float
    macro_f1: float
    passed: bool


@dataclass
class CalibratedSemanticValidator:
    """Calibrate once, then validate each assessment with the same judge."""

    judge: SemanticJudge
    timeout: int = 120
    report: SemanticJudgeCalibration | None = None

    def validate(
        self,
        manifest: RequirementManifest,
        assessment: ScreeningAssessment,
        *,
        screening_provider: str,
    ) -> tuple[SemanticJudgment, ...]:
        if self.report is None:
            self.report = evaluate_semantic_judge(self.judge, timeout=self.timeout)
        if not self.report.passed:
            raise SemanticJudgeError("semantic-eval-threshold-not-met")
        return validate_semantic_assessment(
            manifest, assessment, screening_provider=screening_provider,
            judge=self.judge, timeout=self.timeout,
            expected_judge_provider=self.report.provider,
        )


def build_semantic_claims(
    manifest: RequirementManifest,
    assessment: ScreeningAssessment,
) -> tuple[SemanticClaim, ...]:
    matches = {item.id: item.match for item in assessment.matches}
    evidence = {item.id: item.evidence for item in assessment.matches}
    parent_matches = aggregate_parent_matches(manifest, matches)
    children: dict[str, list[str]] = {}
    for leaf in manifest.leaves:
        children.setdefault(leaf.parent_id or leaf.id, []).append(leaf.id)
    claims: list[SemanticClaim] = []
    for parent in manifest.parents:
        spans: list[str] = []
        for leaf_id in children.get(parent.id, []):
            spans.extend(match.group(1).strip() for match in _CITATION.finditer(evidence[leaf_id]))
        claims.append(SemanticClaim(
            id=parent.id, requirement=parent.text,
            declared_match=parent_matches[parent.id],
            evidence_spans=tuple(dict.fromkeys(spans)),
            kind=parent.kind.value, decisive=parent.decisive,
        ))
    return tuple(claims)


def load_semantic_eval_cases() -> tuple[SemanticEvalCase, ...]:
    raw = resources.files(_EVAL_PACKAGE).joinpath(_EVAL_FILE).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("cases"), list):
        raise SemanticJudgeError("semantic-eval-contract")
    cases = tuple(SemanticEvalCase(
        id=item["id"], group=item["group"], requirement=item["requirement"],
        declared_match=item["declared_match"], evidence_spans=tuple(item["evidence_spans"]),
        expected_label=item["expected_label"], decision_driving=item["decision_driving"],
    ) for item in payload["cases"])
    if len({case.id for case in cases}) != len(cases):
        raise SemanticJudgeError("semantic-eval-duplicate-id")
    group_counts = tuple(sum(case.group == group for case in cases) for group in ("parent-control", "known-overclaim"))
    label_counts = tuple(sum(case.expected_label == label for case in cases) for label in _LABELS)
    adverse_decisions = sum(
        case.decision_driving for case in cases if case.expected_label in _ADVERSE
    )
    if (len(cases), group_counts, label_counts, adverse_decisions) != (21, (14, 7), (6, 6, 8, 1), 2):
        raise SemanticJudgeError("semantic-eval-dataset-contract")
    return cases


def _prompt(items: tuple[SemanticClaim | SemanticEvalCase, ...]) -> str:
    claims = [{
        "id": item.id, "requirement": item.requirement,
        "declared_match": item.declared_match, "evidence_spans": list(item.evidence_spans),
    } for item in items]
    return (
        "각 requirement 전체 문장의 의미와 evidence_spans만 비교하라. "
        "배경지식이나 이력의 다른 부분을 추론하지 마라. "
        "entails는 근거가 요건 전체를 직접 충족할 때만 사용한다. "
        "partial은 일부만 직접 뒷받침할 때 사용한다. "
        "unsupported는 필요한 의미가 근거에 없을 때 사용한다. "
        "contradicted는 근거가 요건을 명시적으로 부정할 때 사용한다. "
        "JSON 객체 하나만 출력한다. 최상위 키는 schema_version과 judgments만 사용한다. "
        "claims 키를 출력하지 않는다. judgments의 각 항목은 id, label, evidence_spans, reason을 포함한다. "
        "evidence_spans에는 입력 문자열을 그대로 복사한다. 입력 배열이 비어 있으면 빈 배열을 출력한다.\n"
        "출력 형태: {\"schema_version\":1,\"judgments\":[{\"id\":\"...\",\"label\":\"entails\",\"evidence_spans\":[\"...\"],\"reason\":\"...\"}]}\n"
        + json.dumps({"schema_version": 1, "claims": claims}, ensure_ascii=False)
    )


def _parse_judgments(
    raw: str,
    items: tuple[SemanticClaim | SemanticEvalCase, ...],
) -> tuple[SemanticJudgment, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SemanticJudgeError("semantic-json-required") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "judgments"}:
        raise SemanticJudgeError("semantic-top-level-contract")
    if payload["schema_version"] != 1 or not isinstance(payload["judgments"], list):
        raise SemanticJudgeError("semantic-schema-contract")
    expected = {item.id: item for item in items}
    judgments: list[SemanticJudgment] = []
    seen: set[str] = set()
    for raw_item in payload["judgments"]:
        if not isinstance(raw_item, dict) or set(raw_item) != {"id", "label", "evidence_spans", "reason"}:
            raise SemanticJudgeError("semantic-item-contract")
        item_id = raw_item["id"]
        if not isinstance(item_id, str) or item_id not in expected or item_id in seen:
            raise SemanticJudgeError("semantic-id-contract")
        label = raw_item["label"]
        if label not in _LABELS:
            raise SemanticJudgeError(f"semantic-label:{item_id}")
        reason = raw_item["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise SemanticJudgeError(f"reason-required:{item_id}")
        spans = raw_item["evidence_spans"]
        if (
            not isinstance(spans, list)
            or any(not isinstance(span, str) for span in spans)
            or (
                not spans
                and (
                    expected[item_id].evidence_spans
                    or expected[item_id].declared_match != "없음"
                )
            )
        ):
            raise SemanticJudgeError(f"evidence-span-required:{item_id}")
        if any(span not in set(expected[item_id].evidence_spans) for span in spans):
            raise SemanticJudgeError(f"evidence-span-not-in-input:{item_id}")
        judgments.append(SemanticJudgment(item_id, label, tuple(spans), " ".join(reason.split())))
        seen.add(item_id)
    if seen != set(expected):
        raise SemanticJudgeError("semantic-id-set")
    return tuple(judgments)


def _provider_family(provider: str) -> str:
    normalized = provider.lower()
    for family in ("claude", "codex", "ollama", "local"):
        if family in normalized:
            return "local" if family in {"ollama", "local"} else family
    return normalized


def _invoke(
    judge: SemanticJudge,
    items: tuple[SemanticClaim | SemanticEvalCase, ...],
    *,
    timeout: int,
) -> tuple[str, tuple[SemanticJudgment, ...]]:
    provider, raw = judge.run(_prompt(items), timeout=timeout, local_timeout=timeout)
    return provider, _parse_judgments(raw, items)


def _f1(expected: list[str], predicted: list[str], label: str) -> float:
    tp = sum(a == label and b == label for a, b in zip(expected, predicted, strict=True))
    fp = sum(a != label and b == label for a, b in zip(expected, predicted, strict=True))
    fn = sum(a == label and b != label for a, b in zip(expected, predicted, strict=True))
    denominator = 2 * tp + fp + fn
    return (2 * tp / denominator) if denominator else 1.0


def evaluate_semantic_judge(
    judge: SemanticJudge,
    *, timeout: int,
    runs: int = 3,
    batch_size: int = 7,
) -> SemanticJudgeCalibration:
    if runs != 3:
        raise ValueError("semantic-eval-runs must be 3")
    if batch_size < 1:
        raise ValueError("semantic-eval-batch-size must be positive")
    cases = load_semantic_eval_cases()
    expected = [case.expected_label for case in cases]
    all_expected: list[str] = []
    all_predicted: list[str] = []
    provider_family: str | None = None
    for _ in range(runs):
        predicted_by_id: dict[str, str] = {}
        for start in range(0, len(cases), batch_size):
            batch = cases[start:start + batch_size]
            provider, judgments = _invoke(judge, batch, timeout=timeout)
            family = _provider_family(provider)
            if provider_family is not None and family != provider_family:
                raise SemanticJudgeError("semantic-eval-provider-changed")
            provider_family = family
            predicted_by_id.update({item.id: item.label for item in judgments})
        all_expected.extend(expected)
        all_predicted.extend(predicted_by_id[case.id] for case in cases)
    adverse = [i for i, label in enumerate(all_expected) if label in _ADVERSE]
    decision = [i for i in adverse if cases[i % len(cases)].decision_driving]
    supported = [i for i, label in enumerate(all_expected) if label in _SUPPORTED]
    adverse_recall = sum(all_predicted[i] in _ADVERSE for i in adverse) / len(adverse)
    decision_recall = sum(all_predicted[i] in _ADVERSE for i in decision) / len(decision)
    supported_acceptance = sum(all_predicted[i] in _SUPPORTED for i in supported) / len(supported)
    macro_f1 = sum(_f1(all_expected, all_predicted, label) for label in _LABELS) / len(_LABELS)
    passed = adverse_recall == 1.0 and decision_recall == 1.0 and supported_acceptance >= 0.95 and macro_f1 >= 0.90
    return SemanticJudgeCalibration(
        provider=provider_family or "unknown", runs=runs,
        adverse_recall=adverse_recall,
        supported_acceptance=supported_acceptance, macro_f1=macro_f1,
        passed=passed,
    )


def validate_semantic_assessment(
    manifest: RequirementManifest,
    assessment: ScreeningAssessment,
    *,
    screening_provider: str,
    judge: SemanticJudge,
    timeout: int,
    expected_judge_provider: str | None = None,
) -> tuple[SemanticJudgment, ...]:
    claims = build_semantic_claims(manifest, assessment)
    judge_provider, judgments = _invoke(judge, claims, timeout=timeout)
    if (
        expected_judge_provider is not None
        and _provider_family(judge_provider) != _provider_family(expected_judge_provider)
    ):
        raise SemanticJudgeError("semantic-judge-provider-drift")
    if _provider_family(judge_provider) == _provider_family(screening_provider):
        raise SemanticJudgeError("independent-provider-required")
    allowed = {
        "충족": frozenset({"entails"}),
        "부분": frozenset({"entails", "partial"}),
        "없음": _ADVERSE,
    }
    judgments_by_id = {judgment.id: judgment for judgment in judgments}
    for claim in claims:
        judgment = judgments_by_id[claim.id]
        if judgment.label not in allowed[claim.declared_match]:
            raise SemanticJudgeError(f"semantic-mismatch:{claim.id}")
    return judgments
