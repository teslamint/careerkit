"""Verdict parsing, normalization, classification, and file movement."""

from __future__ import annotations

import re
from typing import Literal, Optional, List, TypeAlias, cast

from .model import ScreeningVerdict

VERDICT_FOLDER_MAP = {
    "지원 추천": "conditional/high",
    "지원 보류": "conditional/hold",
    "지원 비추천": "pass",
}

VERDICT_PRIORITY = {"지원 비추천": 0, "지원 보류": 1, "지원 추천": 2}

VerdictType: TypeAlias = Literal["지원 추천", "지원 보류", "지원 비추천"]


def normalize_verdict(verdict: str) -> Optional[VerdictType]:
    """Normalize many verdict variants to canonical 3-state values."""
    if not verdict:
        return None

    verdict_clean = re.sub(r"[\*\`_#>\[\]\(\)]", "", verdict).strip()
    verdict_clean = re.sub(r"\s+", " ", verdict_clean)
    verdict_lower = verdict_clean.lower()

    if verdict_clean in {"| 포지션 | 판정 | 사유 |", "포지션 판정 사유", "판정"}:
        return None

    if any(token in verdict_lower for token in ("비추천", "pass", "지원 안 함", "지원안함", "컷", "패스", "not recommend")):
        return "지원 비추천"

    _NEGATED_REVIEW = (
        "검토 대상이 아닙니다", "검토 대상이 아닌", "검토 대상 아님",
        "검토 불필요", "추가 검토 없이", "검토 여지 없",
    )
    if "검토" in verdict_lower and any(neg in verdict_lower for neg in _NEGATED_REVIEW):
        return "지원 비추천"

    if any(token in verdict_lower for token in ("조건부", "보류", "hold", "검토", "킵", "keep", "우선")):
        return "지원 보류"

    if verdict_clean == "\uc9c0\uc6d0":
        return "\uc9c0\uc6d0 \ucd94\ucc9c"

    if any(token in verdict_lower for token in ("\uac15\ub825 \ucd94\ucc9c", "\uc9c0\uc6d0 \ucd94\ucc9c", "\uc989\uc2dc \uc9c0\uc6d0", "\ucd94\ucc9c", "recommend")):
        return "\uc9c0\uc6d0 \ucd94\ucc9c"

    return None


def _section_verdict_candidates(section: str) -> List[VerdictType]:
    """Every normalized verdict a dedicated section body carries."""
    candidates: List[str] = []

    for line in section.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue
        heading_match = re.match(r"^#{1,6}\s*(.+)$", line_stripped)
        if heading_match:
            candidates.append(heading_match.group(1))

        quote_match = re.match(r"^>?\s*판정\s*[:：]\s*(.+)$", line_stripped, re.IGNORECASE)
        if quote_match:
            candidates.append(quote_match.group(1))

        if line_stripped.startswith("|") and line_stripped.endswith("|"):
            cells = [c.strip() for c in line_stripped.strip("|").split("|")]
            if len(cells) >= 2:
                if cells[0] in {"포지션", "position"} and cells[1] in {"판정", "verdict"}:
                    continue
                if re.fullmatch(r"[-:\s]+", cells[0]) and re.fullmatch(r"[-:\s]+", cells[1]):
                    continue
                candidates.append(cells[1])

    normalized = [normalize_verdict(v) for v in candidates]
    return [cast(VerdictType, v) for v in normalized if v is not None]


def classify_by_verdict(verdict: str) -> Optional[str]:
    """Map verdict string to target folder path."""
    normalized = normalize_verdict(verdict)
    if not normalized:
        return None
    return VERDICT_FOLDER_MAP.get(normalized)


def to_screening_verdict(verdict: str) -> ScreeningVerdict | None:
    normalized = normalize_verdict(verdict)
    if normalized == "지원 추천":
        return ScreeningVerdict.RECOMMENDED
    if normalized == "지원 보류":
        return ScreeningVerdict.HOLD
    if normalized == "지원 비추천":
        return ScreeningVerdict.NOT_RECOMMENDED
    return None


# Heading-style verdicts: only explicit 최종 판정 headings on the same line.
# rewrite_verdict_line in application/screening.py rewrites every one of these
# forms — extending this list extends the writer too, plus a round-trip test.
HEADING_VERDICT_PATTERNS = [
    r"^\s*#{1,6}\s*최종\s*판정\s*[:：\-]\s*(.+?)\s*$",
    r"^\s*#{1,6}\s*최종\s*판정[ \t]+(.+?)\s*$",
    r"^\s*-\s*\*\*최종\s*판정\*\*\s*[:：]\s*(.+?)\s*$",
]

# Section-based extraction (handles table and blockquote formats within verdict sections)
_SECTION_PATTERNS = [
    r"(?is)^##\s*최종\s*판정\s*\n(.*?)(?=^##\s|\Z)",
    r"(?is)^##\s*판정\s*\n(.*?)(?=^##\s|\Z)",
]

# Legacy fallback: blockquote/결론/table verdicts (files without ## 최종 판정 section)
_LEGACY_QUOTE_PATTERNS = [
    r"^\s*>\s*판정\s*[:：]\s*(.+?)\s*$",
    r"^\s*>\s*최종\s*판정\s*[:：]\s*(.+?)\s*$",
    r"^\s*\*\*결론\*\*\s*[:：]\s*(.+?)\s*$",
    r"^\s*\|\s*최종\s*판단\s*\|\s*(.+?)\s*\|",
]


def parse_verdict_candidates(screening_content: str) -> List[VerdictType]:
    """Every verdict the reader weighs, from the tier it decides on.

    parse_verdict_from_screening publishes the worst case of this list, which
    means a document can still visibly carry a better verdict the worst-case
    pick papers over. Publication code that rewrites a verdict must therefore
    check this full list, not the single parsed result.
    """
    candidates: List[VerdictType] = []

    for pattern in HEADING_VERDICT_PATTERNS:
        for match in re.finditer(pattern, screening_content, re.IGNORECASE | re.MULTILINE):
            v = normalize_verdict(match.group(1))
            if v:
                candidates.append(v)

    for pattern in _SECTION_PATTERNS:
        for section_match in re.finditer(pattern, screening_content, re.MULTILINE):
            candidates.extend(_section_verdict_candidates(section_match.group(1)))

    if candidates:
        return candidates

    for pattern in _LEGACY_QUOTE_PATTERNS:
        for match in re.finditer(pattern, screening_content, re.IGNORECASE | re.MULTILINE):
            v = normalize_verdict(match.group(1))
            if v:
                candidates.append(v)

    if candidates:
        return candidates

    heading_candidates = re.findall(r"^\s*#{2,6}\s*(.+?)\s*$", screening_content, re.MULTILINE)
    normalized = [normalize_verdict(v) for v in heading_candidates]
    return [cast(VerdictType, v) for v in normalized if v is not None]


def parse_verdict_from_screening(screening_content: str) -> Optional[VerdictType]:
    """Extract canonical verdict from screening analysis content.

    When multiple verdict blocks exist (e.g. re-screened files), returns
    the most conservative (worst-case) verdict for routing safety.
    Collects from both heading-style and section/table verdicts before deciding.
    """
    candidates = parse_verdict_candidates(screening_content)
    if not candidates:
        return None
    return min(candidates, key=lambda v: VERDICT_PRIORITY[v])
