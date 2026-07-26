from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Mapping


class RequirementKind(StrEnum):
    REQUIRED = "필수"
    MAIN_DUTY = "주요업무"
    PREFERRED = "우대"


@dataclass(frozen=True)
class RequirementItem:
    id: str
    text: str
    kind: RequirementKind
    source_heading: str
    source_order: int
    parent_id: str | None = None
    source_span: tuple[int, int] | None = None
    assessable: bool = True
    decisive: bool = False


@dataclass(frozen=True)
class RequirementManifest:
    items: tuple[RequirementItem, ...]
    parents: tuple[RequirementItem, ...]
    leaves: tuple[RequirementItem, ...]
    ambiguous_qualifications: bool = False


@dataclass(frozen=True)
class _ParentDraft:
    heading: str
    kind: RequirementKind
    text: str
    order: int
    decisive: bool
    source_span: tuple[int, int]
    nested_children: tuple[tuple[str, tuple[int, int]], ...] = ()


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
BRACKET_HEADING_RE = re.compile(r"^\[([^\]]+)\]\s*$")
BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*+•◦]\s+(?P<text>.*\S)\s*$")
DELIMITER_RE = re.compile(r",|·|;|/")
ATOMIC_SLASH_RE = re.compile(r"\bCI/CD\b", re.IGNORECASE)
DECISIVE_RE = re.compile(r"(?:\bmust\b|\brequired\b|\bmandatory\b|필수|반드시)", re.IGNORECASE)
ADMINISTRATIVE_RE = re.compile(r"(서류|제출|경력증명|증명서)")
VALID_MATCHES = frozenset({"충족", "부분", "없음"})
SECTION_KINDS = {
    "자격요건": RequirementKind.REQUIRED,
    "필수요건": RequirementKind.REQUIRED,
    "requirements": RequirementKind.REQUIRED,
    "requiredqualifications": RequirementKind.REQUIRED,
    "주요업무": RequirementKind.MAIN_DUTY,
    "담당업무": RequirementKind.MAIN_DUTY,
    "mainresponsibilities": RequirementKind.MAIN_DUTY,
    "whatyouwilldo": RequirementKind.MAIN_DUTY,
    "우대사항": RequirementKind.PREFERRED,
    "preferredqualifications": RequirementKind.PREFERRED,
    "basicqualifications": RequirementKind.REQUIRED,
    "지원자격": RequirementKind.REQUIRED,
    "필수역량": RequirementKind.REQUIRED,
    "keyresponsibilities": RequirementKind.MAIN_DUTY,
    "업무내용": RequirementKind.MAIN_DUTY,
}
SECTION_HEADINGS = {
    RequirementKind.REQUIRED: "자격요건",
    RequirementKind.MAIN_DUTY: "주요업무",
    RequirementKind.PREFERRED: "우대사항",
}
KIND_SLUGS = {
    RequirementKind.REQUIRED: "required",
    RequirementKind.MAIN_DUTY: "main_duty",
    RequirementKind.PREFERRED: "preferred",
}


def _normalize_heading(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9가-힣]+", "", text).lower()
    return cleaned


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    raw = text[start:end]
    piece = raw.strip()
    if not piece:
        return None
    left = start + (len(raw) - len(raw.lstrip()))
    right = end - (len(raw) - len(raw.rstrip()))
    return left, right


def _split_composite_parts(text: str, *, base_offset: int) -> list[tuple[str, tuple[int, int]]]:
    parts: list[tuple[str, tuple[int, int]]] = []
    start = 0
    atomic_slash_positions = {match.start() + 2 for match in ATOMIC_SLASH_RE.finditer(text)}
    for match in DELIMITER_RE.finditer(text):
        if match.start() in atomic_slash_positions:
            continue
        span = _trimmed_span(text, start, match.start())
        if span is not None:
            left, right = span
            parts.append((text[left:right], (base_offset + left, base_offset + right)))
        start = match.end()
    span = _trimmed_span(text, start, len(text))
    if span is not None:
        left, right = span
        parts.append((text[left:right], (base_offset + left, base_offset + right)))
    return parts


def _is_administrative_requirement(text: str) -> bool:
    return bool(ADMINISTRATIVE_RE.search(text))



def _is_information_missing(text: str) -> bool:
    return text.strip() == "정보 없음"


def _make_child_item(
    parent: RequirementItem,
    *,
    index: int,
    text: str,
    span: tuple[int, int] | None,
) -> RequirementItem:
    return RequirementItem(
        id=f"{parent.id}.{index}",
        text=text,
        kind=parent.kind,
        source_heading=parent.source_heading,
        source_order=parent.source_order,
        parent_id=parent.id,
        source_span=span,
        assessable=True,
        decisive=parent.decisive,
    )


def extract_requirement_manifest(jd_markdown: str) -> RequirementManifest:
    drafts: list[_ParentDraft] = []
    current_kind: RequirementKind | None = None
    current_heading = ""
    current_parent_index: int | None = None
    ambiguous_qualifications = False
    section_orders = {
        RequirementKind.REQUIRED: 0,
        RequirementKind.MAIN_DUTY: 0,
        RequirementKind.PREFERRED: 0,
    }

    offset = 0
    for raw_line in jd_markdown.splitlines():
        line_start = offset
        offset += len(raw_line) + 1
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        heading_match = HEADING_RE.match(stripped)
        if heading_match:
            normalized = _normalize_heading(heading_match.group(2))
            current_kind = SECTION_KINDS.get(normalized)
            current_heading = heading_match.group(2).strip()
            current_parent_index = None
            continue

        bracket_match = BRACKET_HEADING_RE.match(line)
        if bracket_match:
            bracket_text = bracket_match.group(1).strip()
            normalized = _normalize_heading(bracket_text)
            new_kind = SECTION_KINDS.get(normalized)
            if new_kind:
                current_kind = new_kind
                current_heading = bracket_text
            else:
                if current_kind is RequirementKind.REQUIRED:
                    ambiguous_qualifications = True
                current_kind = None
            current_parent_index = None
            continue

        if current_kind is None:
            continue

        bullet_match = BULLET_RE.match(line)
        if bullet_match:
            text = bullet_match.group("text").strip()
            indent = len(bullet_match.group("indent"))
            text_start = line_start + bullet_match.start("text")
            text_end = text_start + len(bullet_match.group("text"))
            if _is_information_missing(text) or _is_administrative_requirement(text):
                continue
            if indent > 0 and current_parent_index is not None:
                parent = drafts[current_parent_index]
                drafts[current_parent_index] = _ParentDraft(
                    heading=parent.heading,
                    kind=parent.kind,
                    text=parent.text,
                    order=parent.order,
                    decisive=parent.decisive,
                    source_span=parent.source_span,
                    nested_children=parent.nested_children + ((text, (text_start, text_end)),),
                )
                continue
            section_orders[current_kind] += 1
            drafts.append(
                _ParentDraft(
                    heading=current_heading or SECTION_HEADINGS[current_kind],
                    kind=current_kind,
                    text=text,
                    order=section_orders[current_kind],
                    decisive=current_kind is RequirementKind.REQUIRED and bool(DECISIVE_RE.search(text)),
                    source_span=(text_start, text_end),
                )
            )
            current_parent_index = len(drafts) - 1
            continue

        if current_kind is RequirementKind.REQUIRED and not _is_information_missing(stripped):
            ambiguous_qualifications = True

    items: list[RequirementItem] = []
    parents: list[RequirementItem] = []
    leaves: list[RequirementItem] = []

    for draft in drafts:
        parent = RequirementItem(
            id=f"{KIND_SLUGS[draft.kind]}-{draft.order:03d}",
            text=draft.text,
            kind=draft.kind,
            source_heading=draft.heading,
            source_order=draft.order,
            assessable=True,
            decisive=draft.decisive,
        )
        split_children = _split_composite_parts(
            draft.text,
            base_offset=draft.source_span[0],
        )
        has_composite_children = len(split_children) > 1
        if has_composite_children or draft.nested_children:
            parent = RequirementItem(
                id=parent.id,
                text=parent.text,
                kind=parent.kind,
                source_heading=parent.source_heading,
                source_order=parent.source_order,
                assessable=False,
                decisive=parent.decisive,
            )
        parents.append(parent)
        items.append(parent)

        if has_composite_children:
            for index, (text, span) in enumerate(split_children, start=1):
                child = _make_child_item(parent, index=index, text=text, span=span)
                items.append(child)
                leaves.append(child)

        next_index = len(split_children) + 1
        for offset, (text, span) in enumerate(draft.nested_children, start=next_index):
            child = _make_child_item(parent, index=offset, text=text, span=span)
            items.append(child)
            leaves.append(child)

        if parent.assessable:
            leaves.append(parent)

    return RequirementManifest(
        items=tuple(items),
        parents=tuple(parents),
        leaves=tuple(leaves),
        ambiguous_qualifications=ambiguous_qualifications,
    )


def without_main_duty(manifest: RequirementManifest) -> RequirementManifest:
    return RequirementManifest(
        items=tuple(i for i in manifest.items if i.kind != RequirementKind.MAIN_DUTY),
        parents=tuple(p for p in manifest.parents if p.kind != RequirementKind.MAIN_DUTY),
        leaves=tuple(l for l in manifest.leaves if l.kind != RequirementKind.MAIN_DUTY),
        ambiguous_qualifications=manifest.ambiguous_qualifications,
    )


def aggregate_parent_matches(
    manifest: RequirementManifest,
    leaf_matches: Mapping[str, str],
) -> dict[str, str]:
    parent_matches: dict[str, str] = {}
    children_by_parent: dict[str, list[RequirementItem]] = {}
    for item in manifest.items:
        if item.parent_id is not None:
            children_by_parent.setdefault(item.parent_id, []).append(item)

    for parent in manifest.parents:
        if parent.assessable:
            match = leaf_matches[parent.id]
            if match not in VALID_MATCHES:
                raise ValueError(f"invalid match: {match}")
            parent_matches[parent.id] = match
            continue

        child_matches = [leaf_matches[child.id] for child in children_by_parent.get(parent.id, [])]
        if not child_matches:
            raise ValueError(f"composite parent has no child matches: {parent.id}")
        if any(match not in VALID_MATCHES for match in child_matches):
            raise ValueError(f"invalid child match for parent: {parent.id}")
        if all(match == "충족" for match in child_matches):
            parent_matches[parent.id] = "충족"
        elif all(match == "없음" for match in child_matches):
            parent_matches[parent.id] = "없음"
        else:
            parent_matches[parent.id] = "부분"
    return parent_matches
