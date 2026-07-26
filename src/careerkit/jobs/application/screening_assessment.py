from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from careerkit.jobs.adapters.storage.file_records import StoredJobRecord
from careerkit.jobs.application.requirement_manifest import (
    RequirementItem,
    RequirementManifest,
    aggregate_parent_matches,
)
from careerkit.jobs.domain.verdict import VERDICT_PRIORITY

_MATCH_VALUES = frozenset({"충족", "부분", "없음"})
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "matches",
        "verdict",
        "decision_basis",
        "screening_summary",
        "reasons",
    }
)
_MATCH_KEYS = frozenset({"id", "match", "evidence"})
_DEFAULT_EVIDENCE = "확인 필요"


class AssessmentContractError(ValueError):
    pass


@dataclass(frozen=True)
class AssessmentMatch:
    id: str
    match: str
    evidence: str


@dataclass(frozen=True)
class ScreeningAssessment:
    matches: tuple[AssessmentMatch, ...]
    verdict: str
    decision_basis: tuple[str, ...]
    screening_summary: tuple[str, ...]
    reasons: tuple[str, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise AssessmentContractError(f"duplicate JSON key: {key}")
        parsed[key] = value
    return parsed


def _require_object(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AssessmentContractError("JSON 객체만 허용됩니다") from exc
    if not isinstance(parsed, dict):
        raise AssessmentContractError("JSON 객체만 허용됩니다")
    if frozenset(parsed) != _TOP_LEVEL_KEYS:
        raise AssessmentContractError("unexpected top-level keys")
    return parsed


def _normalize_single_line(value: str) -> str:
    return " ".join(value.split())


def _require_non_empty_strings(value: object, *, field: str, minimum: int, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AssessmentContractError(f"{field} must be a list")
    if not minimum <= len(value) <= maximum:
        raise AssessmentContractError(f"{field} must contain {minimum}-{maximum} non-empty strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AssessmentContractError(f"{field} must contain {minimum}-{maximum} non-empty strings")
        normalized = _normalize_single_line(item)
        if not normalized:
            raise AssessmentContractError(f"{field} must contain {minimum}-{maximum} non-empty strings")
        items.append(normalized)
    return tuple(items)


def _leaf_ids(manifest: RequirementManifest) -> tuple[str, ...]:
    return tuple(item.id for item in manifest.leaves if item.assessable)


def _parent_ids(manifest: RequirementManifest) -> frozenset[str]:
    return frozenset(item.id for item in manifest.parents)


def parse_screening_assessment(raw: str, manifest: RequirementManifest) -> ScreeningAssessment:
    parsed = _require_object(raw)

    schema_version = parsed["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise AssessmentContractError("schema_version must be 1")

    verdict = parsed["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICT_PRIORITY:
        raise AssessmentContractError(f"invalid verdict value: {verdict}")

    matches_raw = parsed["matches"]
    if not isinstance(matches_raw, list):
        raise AssessmentContractError("matches must be a list")

    matches: list[AssessmentMatch] = []
    seen_ids: list[str] = []
    for item in matches_raw:
        if not isinstance(item, dict) or frozenset(item) != _MATCH_KEYS:
            raise AssessmentContractError("unexpected match item keys")
        match_id = item["id"]
        match_value = item["match"]
        evidence = item["evidence"]
        if not isinstance(match_id, str) or not match_id.strip():
            raise AssessmentContractError("match id must be a non-empty string")
        if not isinstance(match_value, str) or match_value not in _MATCH_VALUES:
            raise AssessmentContractError(f"invalid match value: {match_value}")
        if not isinstance(evidence, str):
            raise AssessmentContractError("match evidence must be a non-empty string")
        normalized_evidence = _normalize_single_line(evidence)
        if not normalized_evidence:
            raise AssessmentContractError("match evidence must be a non-empty string")
        seen_ids.append(match_id)
        matches.append(AssessmentMatch(id=match_id, match=match_value, evidence=normalized_evidence))

    expected_ids = _leaf_ids(manifest)
    if tuple(seen_ids) != tuple(dict.fromkeys(seen_ids)) or frozenset(seen_ids) != frozenset(expected_ids) or len(seen_ids) != len(expected_ids):
        raise AssessmentContractError("matches must contain each manifest leaf id exactly once")

    decision_basis_raw = parsed["decision_basis"]
    if not isinstance(decision_basis_raw, list):
        raise AssessmentContractError("decision_basis must be a list")
    decision_basis: list[str] = []
    parent_ids = _parent_ids(manifest)
    for item in decision_basis_raw:
        if not isinstance(item, str) or not item.strip():
            raise AssessmentContractError("decision_basis must reference manifest parent ids")
        if item not in parent_ids:
            raise AssessmentContractError("decision_basis must reference manifest parent ids")
        decision_basis.append(item)
    if len(decision_basis) != len(set(decision_basis)):
        raise AssessmentContractError("decision_basis must reference each manifest parent id at most once")

    screening_summary = _require_non_empty_strings(
        parsed["screening_summary"],
        field="screening_summary",
        minimum=1,
        maximum=4,
    )
    reasons = _require_non_empty_strings(
        parsed["reasons"],
        field="reasons",
        minimum=3,
        maximum=5,
    )

    return ScreeningAssessment(
        matches=tuple(matches),
        verdict=verdict,
        decision_basis=tuple(decision_basis),
        screening_summary=screening_summary,
        reasons=reasons,
    )


def escape_table_cell(value: str | None, default: str = _DEFAULT_EVIDENCE) -> str:
    text = _normalize_single_line(value or default) or default
    return text.replace("|", "\\|")


def _match_map(assessment: ScreeningAssessment) -> dict[str, AssessmentMatch]:
    return {item.id: item for item in assessment.matches}


def _children_by_parent(manifest: RequirementManifest) -> dict[str, list[RequirementItem]]:
    grouped: dict[str, list[RequirementItem]] = {}
    for item in manifest.items:
        if item.parent_id is not None:
            grouped.setdefault(item.parent_id, []).append(item)
    return grouped


def _render_parent_evidence(
    parent: RequirementItem,
    *,
    assessment_map: dict[str, AssessmentMatch],
    child_items: list[RequirementItem],
) -> str:
    if parent.assessable:
        return escape_table_cell(assessment_map[parent.id].evidence)

    parts = [
        f"{child.text} ({assessment_map[child.id].match}): {assessment_map[child.id].evidence}"
        for child in child_items
    ]
    return escape_table_cell(" / ".join(parts))


def render_screening_markdown(
    jd: StoredJobRecord,
    jd_content: str,
    manifest: RequirementManifest,
    assessment: ScreeningAssessment,
) -> str:
    _ = jd_content
    assessment_map = _match_map(assessment)
    parent_matches = aggregate_parent_matches(
        manifest,
        {item.id: item.match for item in assessment.matches},
    )
    children_by_parent = _children_by_parent(manifest)

    lines = [
        "## 기본 정보",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
        f"| 회사명 | {escape_table_cell(jd.record.company)} |",
        f"| 포지션 | {escape_table_cell(jd.record.position)} |",
        "",
        "## 스크리닝 결과",
        "",
    ]
    lines.extend(f"- {item}" for item in assessment.screening_summary)
    lines.extend([
        "",
        "## 이력/경험 매칭",
        "",
        "| 요건 | 구분 | 대조 | 근거 |",
        "|------|------|------|------|",
    ])

    for parent in manifest.parents:
        lines.append(
            "| {requirement} | {kind} | {match} | {evidence} |".format(
                requirement=escape_table_cell(parent.text),
                kind=parent.kind.value,
                match=parent_matches[parent.id],
                evidence=_render_parent_evidence(
                    parent,
                    assessment_map=assessment_map,
                    child_items=children_by_parent.get(parent.id, []),
                ),
            )
        )

    lines.extend([
        "",
        "## 최종 판정",
        "",
        f"### 최종 판정: {assessment.verdict}",
        "",
        "## 핵심 근거",
        "",
    ])
    lines.extend(f"- {item}" for item in assessment.reasons)
    return "\n".join(lines).rstrip() + "\n"
