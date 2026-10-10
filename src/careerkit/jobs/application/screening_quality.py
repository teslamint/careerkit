"""Fail-closed checks for source-backed, internally consistent assessments.

Literal citation checks do not establish semantic entailment. Unstructured
rejection conditions require review instead of heuristic interpretation.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import re
from typing import Mapping

from careerkit.jobs.application.evidence_checks import SOURCE_RE
from careerkit.jobs.application.requirement_manifest import (
    RequirementKind,
    RequirementManifest,
    aggregate_parent_matches,
)
from careerkit.jobs.application.screening_assessment import RULE_BASIS_PREFIX, ScreeningAssessment
from careerkit.jobs.application.search import parse_experience_range


class ScreeningQualityError(ValueError):
    """A completed model response must not replace canonical screening state."""


_GRADE = re.compile(r"^(probable|plausible|possible)\b")
_CITATION = re.compile(r"\[source:\s*([^\]]+)\]\s*\[quote:\s*([^\]]+)\]")
_CONDITION_ID = re.compile(r"\[requirement:\s*([^\]]+)\]")
_CONDITION_RULE = re.compile(r"\[rule:\s*([^\]]+)\]")
# Hold paths that the screening rules define without a requirement row:
# employment type (rules 0.5③) and a △ on final-decision sentences 2, 4, 5.
# Anything else, such as pay, stays manual review.
HOLD_RULE_CONDITIONS = ("employment-type", "leadership-scope", "domain", "experience-cap")
# Final-decision sentences that alone give ❌: 2 (C-level), 4 (non-backend
# domain), 5 (experience cap at or below reject_max). Sentence 1 never applies alone, and
# pay and employment type are not rejection grounds here.
REJECTION_RULE_BASES = ("leadership-scope", "domain", "experience-cap")
_REJECTION_EVIDENCE = re.compile(r"비추천 근거:\s*\[rule:\s*([^\]]+)\]\s*\[quote:\s*([^\]]+)\]")
_COUNT = re.compile(r"(필수|우대|주요업무)\s*(\d+)\s*(?:개|항목)")
_MATCH_COUNT = re.compile(r"(충족|부분|없음)\s*(\d+)")
# Final-decision sentence 5: a stated experience cap at or below one threshold
# is ❌ and a cap at or below a second is △. The model is told the rule but has
# ignored it, so the verdict is checked against the JD here. The thresholds
# come from workspace configuration because they follow the candidate's career.


@dataclass(frozen=True)
class ExperienceCapPolicy:
    reject_max: int
    hold_max: int


# Collected JDs state experience as `| 경력 | … |`, `- **경력**: …`, or `- 경력: …`.
_EXPERIENCE_ROW = re.compile(
    r"^[ \t]*(?:\|[ \t]*경력[ \t]*\||[-*][ \t]*(?:\*\*)?경력(?:\*\*)?[ \t]*:)[ \t]*([^|\n]+)", re.MULTILINE
)


# A quote may join adjacent list items or spell a separator differently from
# the source (`- A, B` / `- C` quoted as `A, B, C` or `- A, B - C`), and may
# drop Markdown bold (`**Admin**:` quoted as `Admin:`). Both sides drop list
# markers, bold marks, and these separators, then the quote must still be one
# contiguous span, so skipped, inserted, or changed words are still rejected.
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+", re.MULTILINE)
_INLINE_MARKER = re.compile(r"(?<!\S)[-*+](?!\S)")
_BOLD = re.compile(r"\*\*")
_SEPARATOR = re.compile(r"[,·/|]")


# JD quotes for rule-basis rejections only tolerate whitespace differences.
def _normalize(text: str) -> str:
    return " ".join(text.split())


def _canonical(text: str) -> str:
    text = _LIST_MARKER.sub("", _BOLD.sub("", text))
    return " ".join(_SEPARATOR.sub(" ", _INLINE_MARKER.sub(" ", text)).split())


def _sources(corpus: str) -> dict[str, list[str]]:
    markers = list(SOURCE_RE.finditer(corpus))
    sources: dict[str, list[str]] = {}
    for index, marker in enumerate(markers):
        path = marker.group(1).strip()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(corpus)
        sources.setdefault(path, []).append(_canonical(corpus[marker.end():end]))
    return sources


def _holds(sources: dict[str, list[str]], path: str, quote: str) -> bool:
    canonical = _canonical(quote)
    return bool(canonical) and any(canonical in block for block in sources.get(path, []))


def _owner(sources: dict[str, list[str]], path: str, quote: str) -> str | None:
    # Only a declared source may be corrected. An absolute, `..`, or unknown
    # path is rejected as written; rewriting it would hide the escape from the
    # containment check.
    if path not in sources:
        return None
    if _holds(sources, path, quote):
        return path
    others = [other for other in sources if other != path and _holds(sources, other, quote)]
    return others[0] if len(others) == 1 else None


def reattribute_citations(assessment: ScreeningAssessment, candidate_context: str) -> ScreeningAssessment:
    """Point each citation at the one source file that holds its quote.

    Models often cite real text under the wrong declared file. When exactly one
    other file holds the quote, the published record names that file. Undeclared
    paths, ambiguous quotes, and unknown quotes are left as written for the gate
    to reject.
    """
    sources = _sources(candidate_context)

    def rewrite(citation: re.Match[str]) -> str:
        owner = _owner(sources, citation.group(1).strip(), citation.group(2))
        if owner is None:
            return citation.group(0)
        start, end = citation.span(1)
        offset = citation.start()
        return citation.group(0)[: start - offset] + owner + citation.group(0)[end - offset:]

    matches = tuple(
        replace(item, evidence=_CITATION.sub(rewrite, item.evidence)) for item in assessment.matches
    )
    if matches == assessment.matches:
        return assessment
    return replace(assessment, matches=matches)


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
    *,
    jd_content: str,
    experience_cap: ExperienceCapPolicy | None = None,
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
            if not _holds(sources, path.strip(), quote):
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
            if item_id.startswith(RULE_BASIS_PREFIX):
                continue
            parent = parents[item_id]
            if parent.kind != RequirementKind.REQUIRED or not parent.decisive or parent_matches[item_id] != "없음":
                issues.append(f"rejection-basis-conflict:{item_id}")
    issues.extend(_rejection_rule_issues(assessment, jd_content))
    if experience_cap is not None:
        issues.extend(_experience_cap_issues(assessment, jd_content, experience_cap))

    narrative = (*assessment.screening_summary, *assessment.reasons)
    promote = [line for line in narrative if line.startswith("추천 전환 조건:")]
    reject = [line for line in narrative if line.startswith("비추천 확정 조건:")]
    if assessment.verdict == "지원 보류" and (len(promote) != 1 or len(reject) != 1):
        issues.append("hold-conditions-required")
    targets = {item.id: item for item in (*manifest.parents, *manifest.leaves)}
    values = {**parent_matches, **matches}
    for line in (*promote, *reject):
        references = _marker_ids(_CONDITION_ID, line)
        rules = _marker_ids(_CONDITION_RULE, line)
        label, _, body = line.partition(":")
        expected = "충족 확인" if label == "추천 전환 조건" else "미충족 확정"
        remainder = _CONDITION_RULE.sub("", _CONDITION_ID.sub("", body)).strip()
        if not (references or rules) or remainder != expected:
            issues.append("condition-manual-review-required")
        if any(rule not in HOLD_RULE_CONDITIONS for rule in rules):
            issues.append("condition-rule-unknown")
        for item_id in references:
            item = targets.get(item_id)
            if item is None or item.kind != RequirementKind.REQUIRED or values.get(item.id) == "충족":
                issues.append("condition-requirement-conflict")
    return tuple(dict.fromkeys(issues))


def _rejection_rule_issues(assessment: ScreeningAssessment, jd_content: str) -> list[str]:
    """Each rule basis needs one quoted JD line; the quote proves presence, not meaning."""
    rules = [item.removeprefix(RULE_BASIS_PREFIX) for item in assessment.decision_basis if item.startswith(RULE_BASIS_PREFIX)]
    issues: list[str] = []
    quotes: dict[str, list[str]] = {}
    for line in (*assessment.screening_summary, *assessment.reasons):
        if not line.startswith("비추천 근거:"):
            continue
        match = _REJECTION_EVIDENCE.fullmatch(line.strip())
        if match is None:
            issues.append("rejection-evidence-malformed")
            continue
        quotes.setdefault(match.group(1).strip(), []).append(_normalize(match.group(2)))
    jd = _normalize(jd_content)
    for rule in rules:
        if rule not in REJECTION_RULE_BASES:
            issues.append(f"rejection-rule-unknown:{rule}")
            continue
        found = quotes.get(rule, [])
        if len(found) != 1:
            issues.append(f"rejection-rule-quote-required:{rule}")
        elif not found[0] or found[0] not in jd:
            issues.append(f"rejection-rule-quote-not-in-jd:{rule}")
    issues.extend(f"rejection-evidence-without-basis:{rule}" for rule in quotes if rule not in rules)
    return issues


def _experience_cap_issues(assessment: ScreeningAssessment, jd_content: str, policy: ExperienceCapPolicy) -> list[str]:
    """Block a verdict the stated experience cap forbids; an unparsed range never trips this."""
    row = _EXPERIENCE_ROW.search(jd_content)
    _, cap = parse_experience_range(row.group(1).strip() if row else None)
    if cap is None:
        return []
    if cap <= policy.reject_max and assessment.verdict != "지원 비추천":
        return ["experience-cap-conflict"]
    if cap <= policy.hold_max and assessment.verdict == "지원 추천":
        return ["experience-cap-conflict"]
    return []


def _marker_ids(pattern: re.Pattern[str], line: str) -> list[str]:
    return [
        item_id.strip()
        for marker in pattern.findall(line)
        for item_id in re.split(r"[,\s]+", marker)
        if item_id.strip()
    ]


def validate_assessment_quality(
    assessment: ScreeningAssessment,
    manifest: RequirementManifest,
    candidate_context: str,
    *,
    jd_content: str,
    experience_cap: ExperienceCapPolicy | None = None,
) -> ScreeningAssessment:
    """Re-attribute citations, then gate the result that will be published."""
    corrected = reattribute_citations(assessment, candidate_context)
    issues = assessment_quality_issues(
        corrected, manifest, candidate_context, jd_content=jd_content, experience_cap=experience_cap
    )
    if issues:
        raise ScreeningQualityError("screening-quality: " + ", ".join(issues))
    return corrected
