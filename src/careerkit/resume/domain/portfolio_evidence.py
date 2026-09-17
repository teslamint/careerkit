"""Strict immutable contracts for private portfolio evidence research."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import cast


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OBSERVED_REF = re.compile(r"^refs/(?:heads|tags|remotes)/[^\s=]+=[0-9a-f]{40,64}$")
_DETACHED_HEAD_REF = re.compile(r"^HEAD=[0-9a-f]{40,64}$")
EVIDENCE_MARKERS = frozenset({"yes", "no", "unknown"})
COMMIT_EXCLUSION_REASONS = frozenset(
    {
        "coauthored-only",
        "duplicate-change",
        "generated-output",
        "inaccessible",
        "merge-only",
        "non-substantive",
        "out-of-scope",
        "vendor-content",
    }
)


def _non_blank(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{field} must be a safe identifier")


def _closed(value: str, allowed: frozenset[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"unknown {field}: {value!r}")


def _aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _relative_path(value: Path, field: str) -> None:
    if not isinstance(value, Path):
        raise ValueError(f"{field} must be a Path")
    pure = PurePosixPath(value.as_posix())
    windows = PureWindowsPath(str(value))
    if (
        value.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"{field} must be a non-traversing relative path")


def _commit_id(value: str, field: str = "commit_id") -> None:
    if not _COMMIT_ID.fullmatch(value):
        raise ValueError(f"{field} must be a 40-64 character lowercase hex object ID")


def _digest(value: str, field: str) -> None:
    if not _DIGEST.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def _unique(values: tuple[str, ...], field: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field}")


def _single_line(value: str, field: str) -> None:
    _non_blank(value, field)
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a single line")


def _markdown_section(value: str, field: str) -> None:
    _non_blank(value, field)
    if value.endswith(("\n", "\r")):
        raise ValueError(f"{field} must not contain a trailing newline")
    if any(line.startswith("## ") for line in value.splitlines()):
        raise ValueError(f"{field} must not inject a Markdown section")


def _validate_observed_refs(values: tuple[str, ...]) -> None:
    for value in values:
        if _DETACHED_HEAD_REF.fullmatch(value):
            continue
        refname = value.split("=", 1)[0]
        if (
            not _OBSERVED_REF.fullmatch(value)
            or ".." in refname
            or "@{" in refname
            or "\\" in refname
            or "//" in refname
            or any(character in refname for character in "~^:?*[")
            or any(ord(character) < 32 or ord(character) == 127 for character in refname)
            or any(component.startswith(".") or component.endswith(".lock") for component in refname.split("/"))
            or refname.endswith(("/", "."))
        ):
            raise ValueError(f"invalid observed_ref: {value!r}")


@dataclass(frozen=True, slots=True)
class AuthorIdentity:
    identity_id: str
    name: str
    email: str

    def __post_init__(self) -> None:
        _identifier(self.identity_id, "identity_id")
        _non_blank(self.name, "name")
        _non_blank(self.email, "email")
        if "@" not in self.email:
            raise ValueError("email must contain @")


@dataclass(frozen=True, slots=True)
class EmploymentPeriod:
    period_id: str
    employment_label: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.period_id, "period_id")
        _non_blank(self.employment_label, "employment_label")
        _aware(self.starts_at, "starts_at")
        _aware(self.ends_at, "ends_at")
        if self.starts_at > self.ends_at:
            raise ValueError("starts_at must not follow ends_at")

    def contains(self, instant: datetime) -> bool:
        _aware(instant, "instant")
        return self.starts_at <= instant <= self.ends_at


@dataclass(frozen=True, slots=True)
class ContextMapping:
    context_id: str
    context_type: str
    discovery_roots: tuple[Path, ...]
    narrative_root: Path
    period_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.context_id, "context_id")
        _closed(self.context_type, frozenset({"company", "personal-open-source"}), "context_type")
        object.__setattr__(self, "discovery_roots", tuple(self.discovery_roots))
        object.__setattr__(self, "period_ids", tuple(self.period_ids))
        if not self.discovery_roots:
            raise ValueError("discovery_roots must not be empty")
        if not self.period_ids:
            raise ValueError("period_ids must not be empty")
        if len(self.discovery_roots) != len(set(self.discovery_roots)):
            raise ValueError("duplicate discovery_root")
        _unique(self.period_ids, "period_id")
        for root in self.discovery_roots:
            if not isinstance(root, Path) or not root.is_absolute():
                raise ValueError("discovery_roots must contain only absolute Paths")
        for period_id in self.period_ids:
            _identifier(period_id, "period_id")
        _relative_path(self.narrative_root, "narrative_root")


@dataclass(frozen=True, slots=True)
class ResearchManifest:
    schema: str
    contexts: tuple[ContextMapping, ...]
    authors: tuple[AuthorIdentity, ...]
    periods: tuple[EmploymentPeriod, ...]

    def __post_init__(self) -> None:
        if self.schema != "portfolio-manifest/v1":
            raise ValueError(f"unknown schema: {self.schema!r}")
        object.__setattr__(self, "contexts", tuple(self.contexts))
        object.__setattr__(self, "authors", tuple(self.authors))
        object.__setattr__(self, "periods", tuple(self.periods))
        if not self.contexts or not self.authors or not self.periods:
            raise ValueError("manifest contexts, authors, and periods must not be empty")
        context_ids = tuple(item.context_id for item in self.contexts)
        author_ids = tuple(item.identity_id for item in self.authors)
        period_ids = tuple(item.period_id for item in self.periods)
        _unique(context_ids, "context_id")
        _unique(author_ids, "identity_id")
        _unique(period_ids, "period_id")
        discovery_roots = tuple(root for context in self.contexts for root in context.discovery_roots)
        if len(discovery_roots) != len(set(discovery_roots)):
            raise ValueError("duplicate discovery_root across contexts")
        known_period_ids = set(period_ids)
        if any(set(context.period_ids) - known_period_ids for context in self.contexts):
            raise ValueError("context references unknown period_id")


@dataclass(frozen=True, slots=True)
class RawRepositoryOccurrence:
    occurrence_id: str
    local_path: Path
    repository_type: str
    git_common_dir: Path | None
    object_store_id: str | None
    observed_refs: tuple[str, ...]
    logical_repository_id: str | None
    exclusion_reason: str | None
    resolution: str

    def __post_init__(self) -> None:
        _identifier(self.occurrence_id, "occurrence_id")
        if not isinstance(self.local_path, Path):
            raise ValueError("local_path must be a Path")
        _closed(self.repository_type, frozenset({"standard", "worktree", "submodule", "bare"}), "repository_type")
        _closed(self.resolution, frozenset({"resolved", "unresolved"}), "resolution")
        object.__setattr__(self, "observed_refs", tuple(self.observed_refs))
        _unique(self.observed_refs, "observed ref")
        _validate_observed_refs(self.observed_refs)
        if self.resolution == "resolved":
            if self.logical_repository_id is None or self.exclusion_reason is not None:
                raise ValueError("resolved occurrence requires only logical_repository_id")
            if not isinstance(self.git_common_dir, Path) or self.object_store_id is None:
                raise ValueError("resolved occurrence requires Git observations")
            _non_blank(self.object_store_id, "object_store_id")
            _identifier(self.logical_repository_id, "logical_repository_id")
        elif (
            self.logical_repository_id is not None
            or not self.exclusion_reason
            or self.git_common_dir is not None
            or self.object_store_id is not None
            or self.observed_refs
        ):
            raise ValueError("unresolved occurrence requires only exclusion_reason and no Git observations")


@dataclass(frozen=True, slots=True)
class LogicalRepository:
    logical_repository_id: str
    occurrence_ids: tuple[str, ...]
    context_id: str
    period_ids: tuple[str, ...]
    author_match: str
    first_commit_id: str | None
    last_commit_id: str | None
    qualifying_commit_count: int
    research_status: str
    deep_research_decision: str
    deep_research_reason: str
    conflict_note: str | None
    observed_refs: tuple[str, ...]
    commit_set_digest: str

    def __post_init__(self) -> None:
        for value, field in ((self.logical_repository_id, "logical_repository_id"), (self.context_id, "context_id")):
            _identifier(value, field)
        object.__setattr__(self, "occurrence_ids", tuple(self.occurrence_ids))
        object.__setattr__(self, "period_ids", tuple(self.period_ids))
        object.__setattr__(self, "observed_refs", tuple(self.observed_refs))
        if not self.occurrence_ids or not self.period_ids:
            raise ValueError("occurrence_ids and period_ids must not be empty")
        _unique(self.occurrence_ids, "occurrence_id")
        _unique(self.period_ids, "period_id")
        _unique(self.observed_refs, "observed ref")
        for period_id in self.period_ids:
            _identifier(period_id, "period_id")
        _validate_observed_refs(self.observed_refs)
        _closed(self.author_match, EVIDENCE_MARKERS, "author_match")
        if self.qualifying_commit_count < 0:
            raise ValueError("qualifying_commit_count must be non-negative")
        if self.qualifying_commit_count == 0:
            if self.first_commit_id is not None or self.last_commit_id is not None:
                raise ValueError("zero-commit repository must omit commit boundaries")
        elif self.first_commit_id is None or self.last_commit_id is None:
            raise ValueError("positive commit count requires both commit boundaries")
        else:
            _commit_id(self.first_commit_id, "first_commit_id")
            _commit_id(self.last_commit_id, "last_commit_id")
        _closed(self.research_status, frozenset({"unscanned", "cataloged", "selected", "excluded", "evidenced", "reconciled", "complete"}), "research_status")
        _closed(self.deep_research_decision, frozenset({"selected", "excluded", "pending"}), "deep_research_decision")
        expected_decision = {
            "unscanned": "pending",
            "cataloged": "pending",
            "selected": "selected",
            "excluded": "excluded",
            "evidenced": "selected",
            "reconciled": "selected",
            "complete": "selected",
        }[self.research_status]
        if self.deep_research_decision != expected_decision:
            raise ValueError("research status and deep-research decision are inconsistent")
        _non_blank(self.deep_research_reason, "deep_research_reason")
        _digest(self.commit_set_digest, "commit_set_digest")

    def allows_transition_to(self, target: "LogicalRepository") -> bool:
        if self.logical_repository_id != target.logical_repository_id:
            return False
        allowed = {
            "unscanned": {"cataloged"},
            "cataloged": {"selected", "excluded"},
            "selected": {"evidenced"},
            "excluded": set(),
            "evidenced": {"reconciled"},
            "reconciled": {"complete"},
            "complete": {"selected"},
        }
        return target.research_status == self.research_status or target.research_status in allowed[self.research_status]


@dataclass(frozen=True, slots=True)
class CommitDisposition:
    logical_repository_id: str
    commit_id: str
    author_identity_id: str
    authored_at: datetime
    period_matches: bool
    merge_marker: str
    generated_output_marker: str
    vendor_marker: str
    coauthor_marker: str
    state: str
    work_unit_ids: tuple[str, ...]
    exclusion_reason: str | None
    evidence_role: str

    def __post_init__(self) -> None:
        _identifier(self.logical_repository_id, "logical_repository_id")
        _commit_id(self.commit_id)
        _identifier(self.author_identity_id, "author_identity_id")
        _aware(self.authored_at, "authored_at")
        for field in ("merge_marker", "generated_output_marker", "vendor_marker", "coauthor_marker"):
            _closed(getattr(self, field), EVIDENCE_MARKERS, field)
        _closed(self.state, frozenset({"unassigned", "work-unit", "excluded"}), "state")
        _closed(self.evidence_role, frozenset({"contribution", "context"}), "evidence_role")
        object.__setattr__(self, "work_unit_ids", tuple(self.work_unit_ids))
        _unique(self.work_unit_ids, "work_unit_id")
        for value in self.work_unit_ids:
            _identifier(value, "work_unit_id")
        if self.state == "work-unit" and (not self.work_unit_ids or self.exclusion_reason is not None):
            raise ValueError("work-unit state requires work_unit_ids and no exclusion_reason")
        if self.state == "excluded" and (self.work_unit_ids or not self.exclusion_reason):
            raise ValueError("excluded state requires exclusion_reason and no work_unit_ids")
        if self.state == "excluded" and self.exclusion_reason not in COMMIT_EXCLUSION_REASONS:
            raise ValueError(f"unknown exclusion_reason: {self.exclusion_reason!r}")
        if self.state == "unassigned" and (self.work_unit_ids or self.exclusion_reason is not None):
            raise ValueError("unassigned state permits no disposition detail")


@dataclass(frozen=True, slots=True)
class EvidenceLink:
    logical_repository_id: str
    commit_id: str
    relative_path: Path
    symbol_or_test: str
    role: str

    def __post_init__(self) -> None:
        _identifier(self.logical_repository_id, "logical_repository_id")
        _commit_id(self.commit_id)
        _relative_path(self.relative_path, "relative_path")
        _non_blank(self.symbol_or_test, "symbol_or_test")
        _closed(self.role, frozenset({"contribution", "context"}), "role")


@dataclass(frozen=True, slots=True)
class DecisionEpisode:
    problem: str
    constraint_or_alternative: str
    selection: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("problem", "constraint_or_alternative", "selection"):
            _single_line(getattr(self, field), field)
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.evidence_ids:
            raise ValueError("decision evidence_ids must not be empty")
        _unique(self.evidence_ids, "decision evidence_id")
        for value in self.evidence_ids:
            _identifier(value, "evidence_id")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    context_id: str
    project_id: str
    work_unit_id: str
    period_ids: tuple[str, ...]
    logical_repository_ids: tuple[str, ...]
    commit_ids: tuple[str, ...]
    attribution_check: str
    period_check: str
    problem: str
    technology_use: str
    personal_contribution: str
    verification: str
    outcome: str
    conflicts_and_exclusions: tuple[str, ...]
    links: tuple[EvidenceLink, ...]
    observed_ref_snapshot: str
    disposition_set_digest: str
    state: str
    decision_type: str
    decision_episode: DecisionEpisode | None = None

    def __post_init__(self) -> None:
        _identifier(self.evidence_id, "evidence_id")
        _identifier(self.context_id, "context_id")
        _identifier(self.project_id, "project_id")
        _identifier(self.work_unit_id, "work_unit_id")
        object.__setattr__(self, "period_ids", tuple(self.period_ids))
        object.__setattr__(self, "logical_repository_ids", tuple(self.logical_repository_ids))
        object.__setattr__(self, "commit_ids", tuple(self.commit_ids))
        object.__setattr__(self, "conflicts_and_exclusions", tuple(self.conflicts_and_exclusions))
        object.__setattr__(self, "links", tuple(self.links))
        if not self.period_ids:
            raise ValueError("evidence period_ids must not be empty")
        if not self.logical_repository_ids or not self.commit_ids or not self.links:
            raise ValueError("evidence repository IDs, commit IDs, and links must not be empty")
        _unique(self.period_ids, "period_id")
        _unique(self.logical_repository_ids, "logical_repository_id")
        _unique(self.commit_ids, "commit_id")
        for value in self.period_ids:
            _identifier(value, "period_id")
        for value in self.logical_repository_ids:
            _identifier(value, "logical_repository_id")
        for value in self.commit_ids:
            _commit_id(value)
        for field in ("attribution_check", "period_check"):
            _closed(getattr(self, field), EVIDENCE_MARKERS, field)
        for field in ("problem", "technology_use", "personal_contribution", "verification", "outcome"):
            _markdown_section(getattr(self, field), field)
        _non_blank(self.observed_ref_snapshot, "observed_ref_snapshot")
        for item in self.conflicts_and_exclusions:
            _single_line(item, "conflict_or_exclusion")
        _digest(self.disposition_set_digest, "disposition_set_digest")
        _closed(self.state, frozenset({"draft", "verified", "conflict", "complete"}), "state")
        _closed(self.decision_type, frozenset({"none", "architecture", "technology", "process", "quality", "operations"}), "decision_type")
        if (self.decision_type == "none") != (self.decision_episode is None):
            raise ValueError("decision_type and decision_episode must agree")
        if self.state == "complete":
            if self.attribution_check != "yes" or self.period_check != "yes":
                raise ValueError("complete evidence requires successful attribution and period checks")
            texts = (
                self.problem,
                self.technology_use,
                self.personal_contribution,
                self.verification,
                self.outcome,
                *self.conflicts_and_exclusions,
            )
            if self.decision_episode is not None:
                texts = (
                    *texts,
                    self.decision_episode.problem,
                    self.decision_episode.constraint_or_alternative,
                    self.decision_episode.selection,
                )
            if any("```" in text or _contains_raw_diff(text) for text in texts):
                raise ValueError("complete evidence rejects code fences and raw diff blocks")
        if {item.logical_repository_id for item in self.links} - set(self.logical_repository_ids):
            raise ValueError("links must reference declared logical_repository_ids")
        if {item.commit_id for item in self.links} - set(self.commit_ids):
            raise ValueError("links must reference declared commit_ids")


@dataclass(frozen=True, slots=True)
class NarrativeProposal:
    proposal_id: str
    source_path: Path
    source_digest: str
    snapshot_path: Path
    snapshot_digest: str
    original_text: str
    proposed_text: str
    evidence_ids: tuple[str, ...]
    state: str
    approved_by: str | None
    approved_at: datetime | None
    applied_output_digest: str | None = None
    review_path: Path | None = None
    review_digest: str | None = None
    supersedes: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.proposal_id, "proposal_id")
        _relative_path(self.source_path, "source_path")
        _digest(self.source_digest, "source_digest")
        _relative_path(self.snapshot_path, "snapshot_path")
        _digest(self.snapshot_digest, "snapshot_digest")
        _non_blank(self.original_text, "original_text")
        _non_blank(self.proposed_text, "proposed_text")
        original_digest = sha256(self.original_text.encode("utf-8")).hexdigest()
        if self.source_digest != original_digest or self.snapshot_digest != original_digest:
            raise ValueError("source and snapshot digests must match original_text")
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.evidence_ids:
            raise ValueError("evidence_ids must not be empty")
        _unique(self.evidence_ids, "evidence_id")
        for value in self.evidence_ids:
            _identifier(value, "evidence_id")
        _closed(self.state, frozenset({"draft", "approved", "applied", "rejected", "superseded"}), "state")
        if self.approved_at is not None:
            _aware(self.approved_at, "approved_at")
        if self.state in {"approved", "applied"} and (not self.approved_by or self.approved_at is None):
            raise ValueError("approved and applied proposals require approval metadata")
        if self.state in {"draft", "rejected", "superseded"} and (self.approved_by is not None or self.approved_at is not None):
            raise ValueError("draft, rejected, and superseded proposals cannot contain approval metadata")
        if self.state == "applied":
            if self.applied_output_digest is None:
                raise ValueError("applied proposal requires applied_output_digest")
            _digest(self.applied_output_digest, "applied_output_digest")
            proposed_digest = sha256(self.proposed_text.encode("utf-8")).hexdigest()
            if self.applied_output_digest != proposed_digest:
                raise ValueError("applied_output_digest must match proposed_text")
        elif self.applied_output_digest is not None:
            raise ValueError("only applied proposal permits applied_output_digest")
        if (self.review_path is None) != (self.review_digest is None):
            raise ValueError("review_path and review_digest must both be present or absent")
        if self.review_path is not None:
            _relative_path(self.review_path, "review_path")
            _digest(cast(str, self.review_digest), "review_digest")
        if self.supersedes is not None:
            _identifier(self.supersedes, "supersedes")
            if self.supersedes == self.proposal_id:
                raise ValueError("proposal must not supersede itself")


@dataclass(frozen=True, slots=True)
class RefreshDecision:
    decision: str
    reason: str

    def __post_init__(self) -> None:
        _closed(self.decision, frozenset({"incremental", "full-reconciliation", "blocked"}), "decision")
        _non_blank(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class PendingWrite:
    target: Path
    original_text: str | None
    replacement_text: str
    pre_state_digest: str | None
    output_digest: str
    state: str = "pending"

    def __post_init__(self) -> None:
        _relative_path(self.target, "target")
        _closed(self.state, frozenset({"pending", "replaced", "compensated", "manual-recovery"}), "write state")
        if (self.original_text is None) != (self.pre_state_digest is None):
            raise ValueError("original_text and pre_state_digest must both be present or absent")
        if self.pre_state_digest is not None and self.original_text is not None:
            _digest(self.pre_state_digest, "pre_state_digest")
            if self.pre_state_digest != sha256(self.original_text.encode("utf-8")).hexdigest():
                raise ValueError("pre_state_digest must match original_text")
        _digest(self.output_digest, "output_digest")
        if self.output_digest != sha256(self.replacement_text.encode("utf-8")).hexdigest():
            raise ValueError("output_digest must match replacement_text")


@dataclass(frozen=True, slots=True)
class PublicationResult:
    transaction_id: str
    state: str
    writes: tuple[PendingWrite, ...]
    journal_path: Path

    def __post_init__(self) -> None:
        _identifier(self.transaction_id, "transaction_id")
        _closed(self.state, frozenset({"prepared", "replacing", "compensating", "committed", "compensated", "manual-recovery"}), "state")
        object.__setattr__(self, "writes", tuple(self.writes))
        if not self.writes:
            raise ValueError("publication journal requires writes")
        _unique(tuple(write.target.as_posix() for write in self.writes), "publication target")
        _relative_path(self.journal_path, "journal_path")
        states = {write.state for write in self.writes}
        ordered_states = tuple(write.state for write in self.writes)
        valid_states = {
            "prepared": states == {"pending"},
            "replacing": _ordered_states(ordered_states, ("replaced", "pending")) and "replaced" in states,
            "compensating": _ordered_states(ordered_states, ("replaced", "compensated", "pending")) and bool(states & {"replaced", "compensated"}),
            "committed": states == {"replaced"},
            "compensated": _ordered_states(ordered_states, ("compensated", "pending")) and "compensated" in states,
            "manual-recovery": states <= {"pending", "replaced", "compensated", "manual-recovery"} and bool(states & {"replaced", "manual-recovery"}),
        }
        if not valid_states[self.state]:
            raise ValueError(f"publication state {self.state!r} is inconsistent with per-write states")

    @property
    def completed_targets(self) -> tuple[Path, ...]:
        return tuple(write.target for write in self.writes if write.state != "pending")

    def blocks(self, target: Path) -> bool:
        if self.state in {"committed", "compensated"}:
            return False
        return any(
            write.target == target
            or tuple(target.parts[-len(write.target.parts) :]) == write.target.parts
            for write in self.writes
        )


def _ordered_states(actual: tuple[str, ...], order: tuple[str, ...]) -> bool:
    ranks = {state: index for index, state in enumerate(order)}
    if any(state not in ranks for state in actual):
        return False
    return tuple(ranks[state] for state in actual) == tuple(sorted(ranks[state] for state in actual))


@dataclass(frozen=True, slots=True)
class EvalCase:
    case_id: str
    category: str
    context_id: str
    prompt: str
    required_claim_ids: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    allowed_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _closed(self.category, frozenset({"supported", "unsupported-distractor", "missing-rationale", "context-separation"}), "category")
        _identifier(self.context_id, "context_id")
        _non_blank(self.prompt, "prompt")
        object.__setattr__(self, "required_claim_ids", tuple(self.required_claim_ids))
        object.__setattr__(self, "forbidden_claims", tuple(self.forbidden_claims))
        object.__setattr__(self, "allowed_evidence_ids", tuple(self.allowed_evidence_ids))
        _unique(self.required_claim_ids, "required_claim_id")
        _unique(self.forbidden_claims, "forbidden_claim")
        _unique(self.allowed_evidence_ids, "allowed_evidence_id")
        for value in self.required_claim_ids:
            _identifier(value, "required_claim_id")
        for value in self.allowed_evidence_ids:
            _identifier(value, "allowed_evidence_id")
        for value in self.forbidden_claims:
            _non_blank(value, "forbidden_claim")


@dataclass(frozen=True, slots=True)
class EvalClaim:
    text: str
    evidence_ids: tuple[str, ...]
    fact_ids: tuple[str, ...]
    context_id: str

    def __post_init__(self) -> None:
        _non_blank(self.text, "text")
        _identifier(self.context_id, "context_id")
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "fact_ids", tuple(self.fact_ids))
        if not self.evidence_ids:
            raise ValueError("evidence_ids must not be empty")
        if not self.fact_ids:
            raise ValueError("fact_ids must not be empty")
        _unique(self.evidence_ids, "evidence_id")
        _unique(self.fact_ids, "fact_id")
        for value in (*self.evidence_ids, *self.fact_ids):
            _identifier(value, "eval claim reference")


@dataclass(frozen=True, slots=True)
class PortfolioEvalOutput:
    case_id: str
    claims: tuple[EvalClaim, ...]
    omitted_claims: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "omitted_claims", tuple(self.omitted_claims))
        if not self.claims and not self.omitted_claims:
            raise ValueError("claims or omitted_claims must not both be empty")
        _unique(self.omitted_claims, "omitted_claim")
        for value in self.omitted_claims:
            _non_blank(value, "omitted_claims")


@dataclass(frozen=True, slots=True)
class DeterministicGrade:
    case_id: str
    issue_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        object.__setattr__(self, "issue_codes", tuple(self.issue_codes))
        _unique(self.issue_codes, "issue_code")
        for value in self.issue_codes:
            _identifier(value, "issue_code")

    @property
    def passed(self) -> bool:
        return not self.issue_codes


@dataclass(frozen=True, slots=True)
class EvalInput:
    kind: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _closed(self.kind, frozenset({"narrative", "index", "evidence", "instruction", "model", "dataset", "grader", "rubric"}), "kind")
        _non_blank(self.version, "version")
        _digest(self.digest, "digest")


@dataclass(frozen=True, slots=True)
class EvalGate:
    gate: str
    inputs: tuple[EvalInput, ...]
    minimum_runs: int
    maximum_errors: int

    def __post_init__(self) -> None:
        _closed(self.gate, frozenset({"smoke", "comprehensive"}), "gate")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if not self.inputs or self.minimum_runs <= 0 or self.maximum_errors < 0:
            raise ValueError("eval gate inputs and thresholds must be valid")


@dataclass(frozen=True, slots=True)
class EvalActivationReceipt:
    run_id: str
    gate: str
    verdict: str
    inputs: tuple[EvalInput, ...]
    completed_at: datetime
    output_count: int
    error_count: int
    result_digest: str

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _closed(self.gate, frozenset({"smoke", "comprehensive"}), "gate")
        _closed(
            self.verdict,
            frozenset({"pass", "fail", "insufficient_data", "unavailable"}),
            "verdict",
        )
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if not self.inputs or len(self.inputs) != len(set(self.inputs)):
            raise ValueError("receipt inputs must be nonempty and unique")
        _aware(self.completed_at, "completed_at")
        if self.output_count < 0 or not 0 <= self.error_count <= self.output_count:
            raise ValueError("receipt counts are invalid")
        if self.verdict == "pass" and (self.output_count == 0 or self.error_count != 0):
            raise ValueError("pass receipt requires outputs with zero errors")
        _digest(self.result_digest, "result_digest")


@dataclass(frozen=True, slots=True)
class EvalComparison:
    correctness_equal: bool
    input_token_regression: float
    output_token_regression: float
    p95_latency_regression: float

    def __post_init__(self) -> None:
        for field in (
            "input_token_regression",
            "output_token_regression",
            "p95_latency_regression",
        ):
            if getattr(self, field) < -1:
                raise ValueError(f"{field} regression must be at least negative one")


@dataclass(frozen=True, slots=True)
class GoldClaim:
    claim_id: str
    label: str
    text: str
    evidence_ids: tuple[str, ...]
    context_id: str

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        _closed(self.label, frozenset({"supported", "unsupported"}), "label")
        _identifier(self.context_id, "context_id")
        _non_blank(self.text, "text")
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if self.label == "supported" and not self.evidence_ids:
            raise ValueError("supported claim requires evidence_ids")
        if self.label == "unsupported" and self.evidence_ids:
            raise ValueError("unsupported claim permits no evidence_ids")
        _unique(self.evidence_ids, "evidence_id")
        for value in self.evidence_ids:
            _identifier(value, "evidence_id")


@dataclass(frozen=True, slots=True)
class Judgment:
    verdict: str
    issue_codes: tuple[str, ...]
    note: str

    def __post_init__(self) -> None:
        _closed(self.verdict, frozenset({"pass", "fail", "insufficient_data", "unavailable"}), "verdict")
        object.__setattr__(self, "issue_codes", tuple(self.issue_codes))
        _unique(self.issue_codes, "issue_code")
        for value in self.issue_codes:
            _identifier(value, "issue_code")


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    run_id: str
    adjudicated_claim_count: int
    agreement: float
    unsupported_claim_recall: float
    cohens_kappa: float
    passed: bool

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        if self.adjudicated_claim_count < 24:
            raise ValueError("calibration requires at least 24 adjudicated claims")
        for field in ("agreement", "unsupported_claim_recall"):
            if not 0 <= getattr(self, field) <= 1:
                raise ValueError(f"{field} must be between zero and one")
        if not -1 <= self.cohens_kappa <= 1:
            raise ValueError("cohens_kappa must be between negative one and one")
        meets_thresholds = self.agreement >= 0.95 and self.unsupported_claim_recall == 1 and self.cohens_kappa >= 0.8
        if self.passed != meets_thresholds:
            raise ValueError("calibration passed state must match declared thresholds")


@dataclass(frozen=True, slots=True)
class EvalResult:
    """A typed result shared by smoke and comprehensive activation gates."""

    run_id: str
    verdict: str
    output_count: int
    error_count: int

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _closed(self.verdict, frozenset({"pass", "fail", "insufficient_data", "unavailable"}), "verdict")
        if self.output_count < 0 or not 0 <= self.error_count <= self.output_count:
            raise ValueError("eval result counts are invalid")
        if self.verdict == "pass" and (self.output_count == 0 or self.error_count != 0):
            raise ValueError("pass result requires outputs with zero errors")

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


@dataclass(frozen=True, slots=True)
class ComprehensiveEvalResult:
    run_id: str
    verdict: str
    output_count: int
    error_count: int
    upper_error_bound: float | None
    per_case_run_counts: tuple[tuple[str, int], ...]
    input_tokens: int | None
    output_tokens: int | None
    max_output_tokens: int | None
    p95_latency_seconds: float | None
    comparable_baseline_regression: float | None
    expected_case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _closed(self.verdict, frozenset({"pass", "fail", "insufficient_data", "unavailable"}), "verdict")
        if self.output_count < 0 or not 0 <= self.error_count <= self.output_count:
            raise ValueError("comprehensive eval counts are invalid")
        object.__setattr__(self, "per_case_run_counts", tuple(self.per_case_run_counts))
        object.__setattr__(self, "expected_case_ids", tuple(self.expected_case_ids))
        case_ids = tuple(case_id for case_id, _ in self.per_case_run_counts)
        _unique(case_ids, "eval case_id")
        _unique(self.expected_case_ids, "expected eval case_id")
        if self.verdict != "unavailable":
            if len(self.expected_case_ids) != 12:
                raise ValueError("available comprehensive results require 12 expected case IDs")
            if set(case_ids) != set(self.expected_case_ids):
                raise ValueError("per-case run counts must match expected case IDs")
        for case_id in self.expected_case_ids:
            _identifier(case_id, "expected case_id")
        for case_id, count in self.per_case_run_counts:
            _identifier(case_id, "case_id")
            if count < 0:
                raise ValueError("per-case run count must be non-negative")
        metrics = (
            self.upper_error_bound,
            self.input_tokens,
            self.output_tokens,
            self.max_output_tokens,
            self.p95_latency_seconds,
            self.comparable_baseline_regression,
        )
        if self.verdict == "unavailable":
            return
        if self.output_count == 0:
            raise ValueError("available comprehensive results require outputs")
        if sum(count for _, count in self.per_case_run_counts) != self.output_count:
            raise ValueError("per-case run counts must sum to output_count")
        if any(value is None for value in metrics):
            raise ValueError("available verdicts require every measured metric")
        upper_bound = cast(float, self.upper_error_bound)
        input_tokens = cast(int, self.input_tokens)
        output_tokens = cast(int, self.output_tokens)
        max_output_tokens = cast(int, self.max_output_tokens)
        latency = cast(float, self.p95_latency_seconds)
        regression = cast(float, self.comparable_baseline_regression)
        if not 0 <= upper_bound <= 1 or min(input_tokens, output_tokens, max_output_tokens) < 0 or latency < 0 or regression < -1:
            raise ValueError("comprehensive eval metrics are outside their valid ranges")
        sufficient_runs = (
            self.output_count >= 60
            and len(self.expected_case_ids) == 12
            and len(self.per_case_run_counts) == 12
            and all(count >= 5 for _, count in self.per_case_run_counts)
        )
        meets_thresholds = (
            sufficient_runs
            and self.error_count == 0
            and upper_bound < 0.05
            and input_tokens <= 1_800_000
            and output_tokens <= 60_000
            and max_output_tokens <= 800
            and latency <= 300
            and regression <= 0.20
        )
        if self.verdict == "pass" and not meets_thresholds:
            raise ValueError("pass verdict requires every R34-R47 threshold")
        if self.verdict == "insufficient_data" and sufficient_runs:
            raise ValueError("insufficient_data verdict requires incomplete run coverage")
        if self.verdict == "fail" and (not sufficient_runs or meets_thresholds):
            raise ValueError("fail verdict requires sufficient runs and a failed threshold")

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def canonical_eval_result_bytes(result: EvalResult | ComprehensiveEvalResult) -> bytes:
    """Serialize an evaluated result for stable digest verification."""
    if isinstance(result, EvalResult):
        payload = {
            "schema": "eval-result/v1",
            "run_id": result.run_id,
            "verdict": result.verdict,
            "output_count": result.output_count,
            "error_count": result.error_count,
        }
    elif isinstance(result, ComprehensiveEvalResult):
        payload = {
            "schema": "comprehensive-eval-result/v1",
            "run_id": result.run_id,
            "verdict": result.verdict,
            "output_count": result.output_count,
            "error_count": result.error_count,
            "upper_error_bound": result.upper_error_bound,
            "per_case_run_counts": [list(item) for item in result.per_case_run_counts],
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "max_output_tokens": result.max_output_tokens,
            "p95_latency_seconds": result.p95_latency_seconds,
            "comparable_baseline_regression": result.comparable_baseline_regression,
            "expected_case_ids": list(result.expected_case_ids),
        }
    else:
        raise TypeError("result must be a typed eval result")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def eval_result_digest(result: EvalResult | ComprehensiveEvalResult) -> str:
    """Return the SHA-256 digest of the canonical typed result bytes."""
    return sha256(canonical_eval_result_bytes(result)).hexdigest()


def _contains_raw_diff(text: str) -> bool:
    return any(
        line.startswith(("diff --git ", "@@ ", "+++ ", "--- "))
        for line in text.splitlines()
    )


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    path: Path | None = None

    def __post_init__(self) -> None:
        _closed(self.severity, frozenset({"error", "warning"}), "severity")
        _identifier(self.code, "code")
        _non_blank(self.message, "message")
        if self.path is not None:
            _relative_path(self.path, "path")


def validate_period_references(
    manifest: ResearchManifest,
    logical_repositories: Sequence[LogicalRepository],
    evidence_records: Sequence[EvidenceRecord],
) -> None:
    """Reject period references outside the approved manifest and repositories."""
    manifest_period_ids = {period.period_id for period in manifest.periods}
    context_by_id = {context.context_id: context for context in manifest.contexts}
    logical_by_id = {item.logical_repository_id: item for item in logical_repositories}
    if len(logical_by_id) != len(logical_repositories):
        raise ValueError("duplicate logical_repository_id")
    for logical in logical_repositories:
        if set(logical.period_ids) - manifest_period_ids:
            raise ValueError("logical repository references unknown period_id")
        context = context_by_id.get(logical.context_id)
        if context is None:
            raise ValueError("logical repository references unknown context_id")
        if set(logical.period_ids) - set(context.period_ids):
            raise ValueError("logical repository period_ids must be present in its context period_ids")
    for evidence in evidence_records:
        if set(evidence.period_ids) - manifest_period_ids:
            raise ValueError("evidence references unknown period_id")
        if evidence.context_id not in context_by_id:
            raise ValueError("evidence references unknown context_id")
        referenced_period_ids: set[str] = set()
        for logical_repository_id in evidence.logical_repository_ids:
            logical = logical_by_id.get(logical_repository_id)
            if logical is None:
                raise ValueError("evidence references unknown logical_repository_id")
            if logical.context_id != evidence.context_id:
                raise ValueError("evidence and logical repository context_id values must match")
            referenced_period_ids.update(logical.period_ids)
        if set(evidence.period_ids) - referenced_period_ids:
            raise ValueError("evidence period_ids must be present in referenced logical repository period_ids")
