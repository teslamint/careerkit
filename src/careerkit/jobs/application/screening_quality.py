"""Fail-closed checks for source-backed, internally consistent assessments.

Literal citation checks do not establish semantic entailment. Unstructured
rejection conditions require review instead of heuristic interpretation.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Mapping

from careerkit.jobs.application.evidence_checks import SOURCE_RE
from careerkit.jobs.application.requirement_manifest import (
    RequirementKind,
    RequirementManifest,
    aggregate_parent_matches,
)
from careerkit.jobs.application.screening_assessment import ScreeningAssessment


class ScreeningQualityError(ValueError):
    """A completed model response must not replace canonical screening state."""


_GRADE = re.compile(r"^(probable|plausible|possible)\b")
_CITATION = re.compile(r"\[source:\s*([^\]]+)\]\s*\[quote:\s*([^\]]+)\]")
_CONDITION_ID = re.compile(r"\[requirement:\s*([^\]]+)\]")
_COUNT = re.compile(r"(필수|우대|주요업무)\s*(\d+)\s*(?:개|항목)")
_MATCH_COUNT = re.compile(r"(충족|부분|없음)\s*(\d+)")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _sources(corpus: str) -> dict[str, list[str]]:
    markers = list(SOURCE_RE.finditer(corpus))
    sources: dict[str, list[str]] = {}
    for index, marker in enumerate(markers):
        path = marker.group(1).strip()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(corpus)
        sources.setdefault(path, []).append(_normalize(corpus[marker.end():end]))
    return sources


def count_quality_issues(
    assessment: ScreeningAssessment,
    manifest: RequirementManifest,
    matches: Mapping[str, str],
) -> tuple[str, ...]:
    issues: list[str] = []
    counts: dict[str, Counter[str]] = {}
    for leaf in manifest.leaves:
        if leaf.assessable:
            counts.setdefault(leaf.kind.value, Counter())[matches[leaf.id]] += 1
    for line in (*assessment.screening_summary, *assessment.reasons):
        declarations = list(_COUNT.finditer(line))
        for index, declaration in enumerate(declarations):
            kind, total = declaration.groups()
            actual = counts.get(kind, Counter())
            end = declarations[index + 1].start() if index + 1 < len(declarations) else len(line)
            details = _MATCH_COUNT.findall(line[declaration.end():end])
            if int(total) != actual.total() or any(int(n) != actual[label] for label, n in details):
                issues.append("count-mismatch")
    return tuple(dict.fromkeys(issues))


def assessment_quality_issues(
    assessment: ScreeningAssessment,
    manifest: RequirementManifest,
    candidate_context: str,
) -> tuple[str, ...]:
    """Return stable issue codes without disclosing source content in errors."""
    sources = _sources(candidate_context)
    matches = {item.id: item.match for item in assessment.matches}
    issues = list(count_quality_issues(assessment, manifest, matches))

    for item in assessment.matches:
        grade_match = _GRADE.match(item.evidence)
        if grade_match is None:
            issues.append(f"evidence-grade:{item.id}")
            continue
        grade = grade_match.group(1)
        if (item.match == "충족" and grade != "probable") or ((grade == "possible") != (item.match == "없음")):
            issues.append(f"evidence-grade-conflict:{item.id}")
        if item.match == "없음":
            continue
        citations = list(_CITATION.finditer(item.evidence))
        if not citations:
            issues.append(f"evidence-citation-required:{item.id}")
            continue
        for citation in citations:
            path, quote = citation.groups()
            quote = _normalize(quote)
            if not quote or not any(quote in block for block in sources.get(path.strip(), [])):
                issues.append(f"evidence-quote-not-in-source:{item.id}")
        # Affirmative evidence contains authenticated excerpts, not additional
        # model-written factual claims that a path-only check cannot verify.
        remainder = _CITATION.sub("", item.evidence[grade_match.end():])
        if remainder.strip(" \t:;,—-"):
            issues.append(f"evidence-unverified-prose:{item.id}")

    parents = {item.id: item for item in manifest.parents}
    parent_matches = aggregate_parent_matches(manifest, matches)
    if assessment.verdict != "지원 비추천" and assessment.decision_basis:
        issues.append("decision-basis-without-rejection")
    if assessment.verdict == "지원 비추천":
        if not assessment.decision_basis:
            issues.append("rejection-basis-required")
        for item_id in assessment.decision_basis:
            parent = parents[item_id]
            if parent.kind != RequirementKind.REQUIRED or not parent.decisive or parent_matches[item_id] != "없음":
                issues.append(f"rejection-basis-conflict:{item_id}")

    narrative = (*assessment.screening_summary, *assessment.reasons)
    promote = [line for line in narrative if line.startswith("추천 전환 조건:")]
    reject = [line for line in narrative if line.startswith("비추천 확정 조건:")]
    if assessment.verdict == "지원 보류" and (len(promote) != 1 or len(reject) != 1):
        issues.append("hold-conditions-required")
    targets = {item.id: item for item in (*manifest.parents, *manifest.leaves)}
    values = {**parent_matches, **matches}
    for line in (*promote, *reject):
        references = [
            item_id.strip()
            for marker in _CONDITION_ID.findall(line)
            for item_id in re.split(r"[,\s]+", marker)
            if item_id.strip()
        ]
        label, _, body = line.partition(":")
        expected = "충족 확인" if label == "추천 전환 조건" else "미충족 확정"
        if not references or _CONDITION_ID.sub("", body).strip() != expected:
            issues.append("condition-manual-review-required")
        for item_id in references:
            item = targets.get(item_id)
            if item is None or item.kind != RequirementKind.REQUIRED or values.get(item.id) == "충족":
                issues.append("condition-requirement-conflict")
    return tuple(dict.fromkeys(issues))


def validate_assessment_quality(
    assessment: ScreeningAssessment,
    manifest: RequirementManifest,
    candidate_context: str,
) -> None:
    issues = assessment_quality_issues(assessment, manifest, candidate_context)
    if issues:
        raise ScreeningQualityError("screening-quality: " + ", ".join(issues))
