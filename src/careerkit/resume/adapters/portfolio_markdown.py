"""Strict Markdown persistence for portfolio evidence contracts."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence, cast

import yaml

from careerkit.resume.domain.portfolio_evidence import (
    AuthorIdentity,
    CommitDisposition,
    ContextMapping,
    DecisionEpisode,
    EmploymentPeriod,
    EvidenceLink,
    EvidenceRecord,
    LogicalRepository,
    NarrativeProposal,
    PendingWrite,
    PublicationResult,
    RawRepositoryOccurrence,
    ResearchManifest,
)


def _frontmatter(data: Mapping[str, Any], body: str = "") -> str:
    rendered = yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{rendered}---\n{body}"


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name} must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{path.name} has unterminated YAML frontmatter")
    try:
        raw = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} has invalid YAML frontmatter: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{path.name} frontmatter must be a string-keyed mapping")
    return cast(dict[str, Any], raw), text[end + 5 :]


def _exact(raw: Mapping[str, Any], fields: set[str], label: str) -> None:
    actual = set(raw)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise ValueError(f"{label} fields must match exactly; missing={missing}, unknown={unknown}")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    return tuple(_string(item, field) for item in _list(value, field))


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a string-keyed mapping")
    return cast(dict[str, Any], value)


def _json_list(value: str, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be a JSON string list") from exc
    return _string_tuple(parsed, field)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _escape_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def _split_row(line: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError("table rows must start and end with a pipe")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line[1:-1]:
        if escaped:
            current.append({"n": "\n"}.get(character, character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append(_decode_cell("".join(current)))
            current = []
        else:
            current.append(character)
    if escaped:
        raise ValueError("table cell has an incomplete escape")
    cells.append(_decode_cell("".join(current)))
    return cells


def _decode_cell(value: str) -> str:
    if not value.startswith(" ") or not value.endswith(" "):
        raise ValueError("table cells require one structural surrounding space")
    return value[1:-1]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    rendered_rows = ["| " + " | ".join(_escape_cell(value) for value in row) + " |" for row in rows]
    return "\n".join((header, separator, *rendered_rows)) + "\n"


def _parse_table(
    body: str,
    heading: str,
    headers: Sequence[str],
    *,
    next_heading: str | None = None,
) -> list[list[str]]:
    prefix = f"## {heading}\n\n"
    start = body.find(prefix)
    if start < 0:
        raise ValueError(f"missing {heading} table")
    lines = body[start + len(prefix) :].splitlines()
    if len(lines) < 2:
        raise ValueError(f"{heading} table is incomplete")
    actual_headers = _split_row(lines[0])
    if actual_headers != list(headers):
        raise ValueError(f"{heading} columns must match exactly")
    separators = _split_row(lines[1])
    if separators != ["---"] * len(headers):
        raise ValueError(f"{heading} separator columns must match exactly")
    rows: list[list[str]] = []
    for index, line in enumerate(lines[2:], 2):
        if not line:
            remaining = lines[index + 1 :]
            if next_heading is None:
                if remaining:
                    raise ValueError(f"{heading} table has unconsumed content")
            elif not remaining or remaining[0] != f"## {next_heading}":
                raise ValueError(f"{heading} table has unconsumed content")
            break
        if line.startswith("## "):
            raise ValueError(f"{heading} table has unconsumed content")
        cells = _split_row(line)
        if len(cells) != len(headers):
            raise ValueError(f"{heading} row columns must match exactly")
        rows.append(cells)
    return rows


def _author_data(value: AuthorIdentity) -> dict[str, str]:
    return {"identity_id": value.identity_id, "name": value.name, "email": value.email}


def _period_data(value: EmploymentPeriod) -> dict[str, str]:
    return {"period_id": value.period_id, "employment_label": value.employment_label, "starts_at": _iso(value.starts_at), "ends_at": _iso(value.ends_at)}


def _context_data(value: ContextMapping) -> dict[str, Any]:
    return {
        "context_id": value.context_id,
        "context_type": value.context_type,
        "discovery_roots": [str(root) for root in value.discovery_roots],
        "narrative_root": value.narrative_root.as_posix(),
        "period_ids": list(value.period_ids),
    }


def render_manifest(manifest: ResearchManifest) -> str:
    return _frontmatter(
        {
            "schema": manifest.schema,
            "contexts": [_context_data(item) for item in manifest.contexts],
            "authors": [_author_data(item) for item in manifest.authors],
            "periods": [_period_data(item) for item in manifest.periods],
        },
        "# Portfolio research manifest\n",
    )


def load_manifest(
    path: Path,
    *,
    journals: Sequence[PublicationResult] = (),
) -> ResearchManifest:
    ensure_readable(path, journals)
    raw, body = _parse_frontmatter(path)
    _exact(raw, {"schema", "contexts", "authors", "periods"}, "manifest")
    if body != "# Portfolio research manifest\n":
        raise ValueError("manifest body must match the canonical heading")
    contexts: list[ContextMapping] = []
    for index, item in enumerate(_list(raw["contexts"], "contexts")):
        data = _mapping(item, f"contexts[{index}]")
        _exact(data, {"context_id", "context_type", "discovery_roots", "narrative_root", "period_ids"}, "context")
        discovery_roots = tuple(Path(_string(root, "discovery_root")) for root in _list(data["discovery_roots"], "discovery_roots"))
        contexts.append(ContextMapping(_string(data["context_id"], "context_id"), _string(data["context_type"], "context_type"), discovery_roots, Path(_string(data["narrative_root"], "narrative_root")), _string_tuple(data["period_ids"], "period_ids")))
    authors: list[AuthorIdentity] = []
    for index, item in enumerate(_list(raw["authors"], "authors")):
        data = _mapping(item, f"authors[{index}]")
        _exact(data, {"identity_id", "name", "email"}, "author")
        authors.append(AuthorIdentity(_string(data["identity_id"], "identity_id"), _string(data["name"], "name"), _string(data["email"], "email")))
    periods: list[EmploymentPeriod] = []
    for index, item in enumerate(_list(raw["periods"], "periods")):
        data = _mapping(item, f"periods[{index}]")
        _exact(data, {"period_id", "employment_label", "starts_at", "ends_at"}, "period")
        periods.append(EmploymentPeriod(_string(data["period_id"], "period_id"), _string(data["employment_label"], "employment_label"), _datetime(data["starts_at"], "starts_at"), _datetime(data["ends_at"], "ends_at")))
    return ResearchManifest(_string(raw["schema"], "schema"), tuple(contexts), tuple(authors), tuple(periods))


_OCCURRENCE_HEADERS = ("Occurrence ID", "Local path", "Repository type", "Git common dir", "Object store ID", "Observed refs", "Logical repository ID", "Exclusion reason", "Resolution")
_LOGICAL_HEADERS = ("Logical repository ID", "Occurrence IDs", "Context ID", "Period IDs", "Author match", "First commit", "Last commit", "Commit count", "Research status", "Deep research decision", "Deep research reason", "Conflict note", "Observed refs", "Commit-set digest")


def render_catalog(occurrences: Sequence[RawRepositoryOccurrence], logical_repositories: Sequence[LogicalRepository]) -> str:
    _validate_catalog_references(occurrences, logical_repositories)
    occurrence_rows = [[item.occurrence_id, str(item.local_path), item.repository_type, str(item.git_common_dir or ""), item.object_store_id or "", json.dumps(item.observed_refs, ensure_ascii=False), item.logical_repository_id or "", item.exclusion_reason or "", item.resolution] for item in occurrences]
    logical_rows = [[item.logical_repository_id, json.dumps(item.occurrence_ids), item.context_id, json.dumps(item.period_ids), item.author_match, item.first_commit_id or "", item.last_commit_id or "", str(item.qualifying_commit_count), item.research_status, item.deep_research_decision, item.deep_research_reason, item.conflict_note or "", json.dumps(item.observed_refs, ensure_ascii=False), item.commit_set_digest] for item in logical_repositories]
    body = f"# Portfolio repository catalog\n\n## Raw occurrences\n\n{_table(_OCCURRENCE_HEADERS, occurrence_rows)}\n## Logical repositories\n\n{_table(_LOGICAL_HEADERS, logical_rows)}"
    return _frontmatter({"schema": "portfolio-catalog/v1"}, body)


def load_catalog(
    path: Path,
    *,
    journals: Sequence[PublicationResult] = (),
) -> tuple[list[RawRepositoryOccurrence], list[LogicalRepository]]:
    ensure_readable(path, journals)
    raw, body = _parse_frontmatter(path)
    _exact(raw, {"schema"}, "catalog")
    if raw["schema"] != "portfolio-catalog/v1":
        raise ValueError(f"unknown schema: {raw['schema']!r}")
    occurrences = [RawRepositoryOccurrence(row[0], Path(row[1]), row[2], Path(row[3]) if row[3] else None, row[4] or None, _json_list(row[5], "observed_refs"), row[6] or None, row[7] or None, row[8]) for row in _parse_table(body, "Raw occurrences", _OCCURRENCE_HEADERS, next_heading="Logical repositories")]
    logical = [LogicalRepository(row[0], _json_list(row[1], "occurrence_ids"), row[2], _json_list(row[3], "period_ids"), row[4], row[5] or None, row[6] or None, _integer_from_text(row[7], "qualifying_commit_count"), row[8], row[9], row[10], row[11] or None, _json_list(row[12], "observed_refs"), row[13]) for row in _parse_table(body, "Logical repositories", _LOGICAL_HEADERS)]
    _validate_catalog_references(occurrences, logical)
    return occurrences, logical


def _integer_from_text(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _reject_duplicate_ids(values: Any, field: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"duplicate {field}")


def _validate_catalog_references(
    occurrences: Sequence[RawRepositoryOccurrence],
    logical_repositories: Sequence[LogicalRepository],
) -> None:
    _reject_duplicate_ids((item.occurrence_id for item in occurrences), "occurrence_id")
    _reject_duplicate_ids(
        (item.logical_repository_id for item in logical_repositories),
        "logical_repository_id",
    )
    occurrence_by_id = {item.occurrence_id: item for item in occurrences}
    logical_by_id = {item.logical_repository_id: item for item in logical_repositories}
    owners: dict[str, int] = {}
    for logical in logical_repositories:
        unknown = set(logical.occurrence_ids) - set(occurrence_by_id)
        if unknown:
            raise ValueError("logical repository references unknown occurrence_ids")
        for occurrence_id in logical.occurrence_ids:
            owners[occurrence_id] = owners.get(occurrence_id, 0) + 1
    for occurrence in occurrences:
        owner_count = owners.get(occurrence.occurrence_id, 0)
        if occurrence.resolution == "unresolved":
            if owner_count:
                raise ValueError("unresolved occurrence must appear in no logical repository")
            continue
        if owner_count != 1:
            raise ValueError("resolved occurrence must map to exactly one logical repository")
        logical_id = cast(str, occurrence.logical_repository_id)
        logical = logical_by_id.get(logical_id)
        if logical is None or occurrence.occurrence_id not in logical.occurrence_ids:
            raise ValueError("resolved occurrence has an inconsistent logical repository reference")


_DISPOSITION_HEADERS = ("Logical repository ID", "Commit ID", "Author identity ID", "Authored at", "Period matches", "Merge", "Generated output", "Vendor", "Coauthor", "State", "Work-unit IDs", "Exclusion reason", "Evidence role")


def render_dispositions(dispositions: Sequence[CommitDisposition]) -> str:
    _reject_duplicate_ids(
        ((item.logical_repository_id, item.commit_id) for item in dispositions),
        "commit disposition",
    )
    rows = [[item.logical_repository_id, item.commit_id, item.author_identity_id, _iso(item.authored_at), json.dumps(item.period_matches), item.merge_marker, item.generated_output_marker, item.vendor_marker, item.coauthor_marker, item.state, json.dumps(item.work_unit_ids), item.exclusion_reason or "", item.evidence_role] for item in dispositions]
    return _frontmatter({"schema": "portfolio-dispositions/v1"}, f"# Commit dispositions\n\n## Dispositions\n\n{_table(_DISPOSITION_HEADERS, rows)}")


def load_dispositions(
    path: Path,
    *,
    journals: Sequence[PublicationResult] = (),
) -> list[CommitDisposition]:
    ensure_readable(path, journals)
    raw, body = _parse_frontmatter(path)
    _exact(raw, {"schema"}, "dispositions")
    if raw["schema"] != "portfolio-dispositions/v1":
        raise ValueError(f"unknown schema: {raw['schema']!r}")
    result = [CommitDisposition(row[0], row[1], row[2], _datetime(row[3], "authored_at"), _json_boolean(row[4], "period_matches"), row[5], row[6], row[7], row[8], row[9], _json_list(row[10], "work_unit_ids"), row[11] or None, row[12]) for row in _parse_table(body, "Dispositions", _DISPOSITION_HEADERS)]
    _reject_duplicate_ids(((item.logical_repository_id, item.commit_id) for item in result), "commit disposition")
    return result


def _json_boolean(value: str, field: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be a JSON boolean") from exc
    return _boolean(parsed, field)


_LINK_HEADERS = ("Logical repository ID", "Commit ID", "Relative path", "Symbol or test", "Evidence role")


def render_evidence(evidence: EvidenceRecord) -> str:
    data = {
        "schema": "portfolio-evidence/v1",
        "evidence_id": evidence.evidence_id,
        "context_id": evidence.context_id,
        "project_id": evidence.project_id,
        "work_unit_id": evidence.work_unit_id,
        "period_ids": list(evidence.period_ids),
        "logical_repository_ids": list(evidence.logical_repository_ids),
        "commit_ids": list(evidence.commit_ids),
        "attribution_check": evidence.attribution_check,
        "period_check": evidence.period_check,
        "observed_ref_snapshot": evidence.observed_ref_snapshot,
        "disposition_set_digest": evidence.disposition_set_digest,
        "state": evidence.state,
        "decision_type": evidence.decision_type,
    }
    rows = [[link.logical_repository_id, link.commit_id, link.relative_path.as_posix(), link.symbol_or_test, link.role] for link in evidence.links]
    decision = _render_decision(evidence.decision_episode)
    conflicts = "\n".join(f"- {item}" for item in evidence.conflicts_and_exclusions) or "None."
    body = (
        "# Evidence ledger record\n\n"
        f"## Problem\n\n{evidence.problem}\n\n"
        f"## Technology use\n\n{evidence.technology_use}\n\n"
        f"## Contribution\n\n{evidence.personal_contribution}\n\n"
        f"## Verification\n\n{evidence.verification}\n\n"
        f"## Outcome\n\n{evidence.outcome}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Conflicts and exclusions\n\n{conflicts}\n\n"
        f"## Evidence links\n\n{_table(_LINK_HEADERS, rows)}"
    )
    return _frontmatter(data, body)


def load_evidence(
    path: Path,
    *,
    journals: Sequence[PublicationResult] = (),
) -> EvidenceRecord:
    ensure_readable(path, journals)
    raw, body = _parse_frontmatter(path)
    fields = {"schema", "evidence_id", "context_id", "project_id", "work_unit_id", "period_ids", "logical_repository_ids", "commit_ids", "attribution_check", "period_check", "observed_ref_snapshot", "disposition_set_digest", "state", "decision_type"}
    _exact(raw, fields, "evidence")
    if raw["schema"] != "portfolio-evidence/v1":
        raise ValueError(f"unknown schema: {raw['schema']!r}")
    links = tuple(EvidenceLink(row[0], row[1], Path(row[2]), row[3], row[4]) for row in _parse_table(body, "Evidence links", _LINK_HEADERS))
    sections = _parse_evidence_sections(body)
    conflicts = () if sections["Conflicts and exclusions"] == "None." else tuple(_parse_bullets(sections["Conflicts and exclusions"]))
    decision = _parse_decision(sections["Decision"])
    return EvidenceRecord(_string(raw["evidence_id"], "evidence_id"), _string(raw["context_id"], "context_id"), _string(raw["project_id"], "project_id"), _string(raw["work_unit_id"], "work_unit_id"), _string_tuple(raw["period_ids"], "period_ids"), _string_tuple(raw["logical_repository_ids"], "logical_repository_ids"), _string_tuple(raw["commit_ids"], "commit_ids"), _string(raw["attribution_check"], "attribution_check"), _string(raw["period_check"], "period_check"), sections["Problem"], sections["Technology use"], sections["Contribution"], sections["Verification"], sections["Outcome"], conflicts, links, _string(raw["observed_ref_snapshot"], "observed_ref_snapshot"), _string(raw["disposition_set_digest"], "disposition_set_digest"), _string(raw["state"], "state"), _string(raw["decision_type"], "decision_type"), decision)


def _render_decision(decision: DecisionEpisode | None) -> str:
    if decision is None:
        return "None."
    return "\n".join((f"- Problem: {decision.problem}", f"- Constraint or alternative: {decision.constraint_or_alternative}", f"- Selection: {decision.selection}", f"- Evidence IDs: {json.dumps(decision.evidence_ids)}"))


def _parse_decision(text: str) -> DecisionEpisode | None:
    if text == "None.":
        return None
    lines = text.splitlines()
    prefixes = ("- Problem: ", "- Constraint or alternative: ", "- Selection: ", "- Evidence IDs: ")
    if len(lines) != len(prefixes) or any(not line.startswith(prefix) for line, prefix in zip(lines, prefixes, strict=True)):
        raise ValueError("Decision section must use the exact fixed shape")
    return DecisionEpisode(lines[0][len(prefixes[0]):], lines[1][len(prefixes[1]):], lines[2][len(prefixes[2]):], _json_list(lines[3][len(prefixes[3]):], "decision evidence_ids"))


def _parse_evidence_sections(body: str) -> dict[str, str]:
    parts = body.split("\n\n## ")
    if not parts or parts[0] != "# Evidence ledger record":
        raise ValueError("evidence body must start with the canonical heading")
    expected = ["Problem", "Technology use", "Contribution", "Verification", "Outcome", "Decision", "Conflicts and exclusions", "Evidence links"]
    result: dict[str, str] = {}
    for part, heading in zip(parts[1:], expected, strict=True):
        prefix = f"{heading}\n\n"
        if not part.startswith(prefix):
            raise ValueError("evidence sections must match the exact order")
        content = part[len(prefix):]
        if heading == "Evidence links":
            if not content.endswith("\n") or content.endswith("\n\n"):
                raise ValueError("Evidence links section must end with one canonical newline")
            result[heading] = content[:-1]
        else:
            if content.endswith(("\n", "\r")):
                raise ValueError(f"{heading} section has non-canonical trailing whitespace")
            result[heading] = content
    if len(parts) - 1 != len(expected):
        raise ValueError("evidence sections must match the exact set")
    return result


def _parse_bullets(text: str) -> list[str]:
    lines = text.splitlines()
    if any(not line.startswith("- ") for line in lines):
        raise ValueError("conflicts must use Markdown bullets")
    return [line[2:] for line in lines]


def render_proposal(proposal: NarrativeProposal) -> str:
    is_v2 = proposal.review_path is not None or proposal.supersedes is not None
    data: dict[str, Any] = {
        "schema": "portfolio-proposal/v2" if is_v2 else "portfolio-proposal/v1",
        "proposal_id": proposal.proposal_id,
        "source_path": proposal.source_path.as_posix(),
        "source_digest": proposal.source_digest,
        "snapshot_path": proposal.snapshot_path.as_posix(),
        "snapshot_digest": proposal.snapshot_digest,
        "original_text": proposal.original_text,
        "proposed_text": proposal.proposed_text,
        "evidence_ids": list(proposal.evidence_ids),
        "state": proposal.state,
        "approved_by": proposal.approved_by,
        "approved_at": _iso(proposal.approved_at) if proposal.approved_at is not None else None,
        "applied_output_digest": proposal.applied_output_digest,
    }
    if is_v2:
        data.update(
            {
                "review_path": proposal.review_path.as_posix() if proposal.review_path is not None else None,
                "review_digest": proposal.review_digest,
                "supersedes": proposal.supersedes,
            }
        )
    return _frontmatter(
        data,
        "# Narrative proposal\n",
    )


def load_proposal(
    path: Path,
    *,
    journals: Sequence[PublicationResult] = (),
) -> NarrativeProposal:
    ensure_readable(path, journals)
    raw, body = _parse_frontmatter(path)
    fields = {"schema", "proposal_id", "source_path", "source_digest", "snapshot_path", "snapshot_digest", "original_text", "proposed_text", "evidence_ids", "state", "approved_by", "approved_at", "applied_output_digest"}
    schema = _string(raw.get("schema"), "schema")
    if schema == "portfolio-proposal/v2":
        fields |= {"review_path", "review_digest", "supersedes"}
    _exact(raw, fields, "proposal")
    if schema not in {"portfolio-proposal/v1", "portfolio-proposal/v2"}:
        raise ValueError(f"unknown schema: {raw['schema']!r}")
    if body != "# Narrative proposal\n":
        raise ValueError("proposal body must match the canonical heading")
    approved_at = None if raw["approved_at"] is None else _datetime(raw["approved_at"], "approved_at")
    review_path = None if schema == "portfolio-proposal/v1" or raw["review_path"] is None else Path(_string(raw["review_path"], "review_path"))
    review_digest = None if schema == "portfolio-proposal/v1" else _optional_string(raw["review_digest"], "review_digest")
    supersedes = None if schema == "portfolio-proposal/v1" else _optional_string(raw["supersedes"], "supersedes")
    return NarrativeProposal(_string(raw["proposal_id"], "proposal_id"), Path(_string(raw["source_path"], "source_path")), _string(raw["source_digest"], "source_digest"), Path(_string(raw["snapshot_path"], "snapshot_path")), _string(raw["snapshot_digest"], "snapshot_digest"), _string(raw["original_text"], "original_text"), _string(raw["proposed_text"], "proposed_text"), _string_tuple(raw["evidence_ids"], "evidence_ids"), _string(raw["state"], "state"), _optional_string(raw["approved_by"], "approved_by"), approved_at, _optional_string(raw["applied_output_digest"], "applied_output_digest"), review_path, review_digest, supersedes)


def render_publication_result(result: PublicationResult) -> str:
    writes = [
        {
            "target": write.target.as_posix(),
            "original_text": write.original_text,
            "replacement_text": write.replacement_text,
            "pre_state_digest": write.pre_state_digest,
            "output_digest": write.output_digest,
            "state": write.state,
        }
        for write in result.writes
    ]
    return _frontmatter(
        {
            "schema": "portfolio-publication/v1",
            "transaction_id": result.transaction_id,
            "state": result.state,
            "journal_path": result.journal_path.as_posix(),
            "writes": writes,
        },
        "# Publication journal\n",
    )


def load_publication_result(path: Path) -> PublicationResult:
    raw, body = _parse_frontmatter(path)
    _exact(raw, {"schema", "transaction_id", "state", "journal_path", "writes"}, "publication journal")
    if raw["schema"] != "portfolio-publication/v1":
        raise ValueError(f"unknown schema: {raw['schema']!r}")
    if body != "# Publication journal\n":
        raise ValueError("publication journal body must match the canonical heading")
    writes: list[PendingWrite] = []
    for index, item in enumerate(_list(raw["writes"], "writes")):
        data = _mapping(item, f"writes[{index}]")
        _exact(
            data,
            {"target", "original_text", "replacement_text", "pre_state_digest", "output_digest", "state"},
            "pending write",
        )
        writes.append(
            PendingWrite(
                Path(_string(data["target"], "target")),
                _optional_string(data["original_text"], "original_text"),
                _string(data["replacement_text"], "replacement_text"),
                _optional_string(data["pre_state_digest"], "pre_state_digest"),
                _string(data["output_digest"], "output_digest"),
                _string(data["state"], "state"),
            )
        )
    return PublicationResult(
        _string(raw["transaction_id"], "transaction_id"),
        _string(raw["state"], "state"),
        tuple(writes),
        Path(_string(raw["journal_path"], "journal_path")),
    )


class AtomicWriteCommitUncertain(OSError):
    def __init__(self, path: Path, output_digest: str, cause: OSError):
        super().__init__(f"replacement committed but durability is uncertain: {path}")
        self.path = path
        self.output_digest = output_digest
        self.__cause__ = cause


def ensure_readable(target: Path, journals: Sequence[PublicationResult]) -> None:
    if any(journal.blocks(target) for journal in journals):
        raise ValueError(
            f"blocking publication journal requires manual recovery before reading target: {target}"
        )


def atomic_write(path: Path, text: str) -> None:
    """Durably replace *path* using a same-directory temporary file."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("atomic write target must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary_path, path)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            if not temporary_path.exists():
                raise AtomicWriteCommitUncertain(path, sha256(text.encode("utf-8")).hexdigest(), exc) from exc
            raise
    finally:
        temporary_path.unlink(missing_ok=True)
