"""Application services for private portfolio research inventory."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from collections.abc import Mapping, Sequence, Set

import yaml

from careerkit.resume.adapters.git_portfolio import (
    CommitObservation,
    GitPortfolioInspector,
    GitPortfolioIssue,
    RepositoryObservation,
    repository_group_key,
)
from careerkit.resume.adapters.portfolio_markdown import (
    atomic_write,
    ensure_readable,
    load_catalog,
    load_dispositions,
    load_evidence,
    load_manifest,
    load_proposal,
    load_publication_result,
    render_proposal,
    render_dispositions,
    render_publication_result,
)
from careerkit.resume.domain.portfolio_evidence import (
    CalibrationResult,
    CommitDisposition,
    DeterministicGrade,
    ComprehensiveEvalResult,
    EvalActivationReceipt,
    EvalComparison,
    EvalGate,
    EvalCase,
    EvalResult,
    eval_result_digest,
    PortfolioEvalOutput,
    EvalInput,
    EvidenceRecord,
    GoldClaim,
    Judgment,
    LogicalRepository,
    NarrativeProposal,
    PendingWrite,
    PublicationResult,
    RawRepositoryOccurrence,
    RefreshDecision,
    ResearchManifest,
    ValidationIssue,
    validate_period_references,
)


_PROPOSAL_EVIDENCE_REFERENCE = re.compile(r"\[Evidence:\s*([^\]]+)\]")
_PROPOSAL_EVIDENCE_REFERENCE_AT_LINE_END = re.compile(r"(?:\[Evidence:\s*[^\]]+\]\s*)+$")
_PROPOSAL_REVIEW_BOUNDARY_DISCLAIMER = (
    "This draft replaces unsupported precision with ledger-backed observations. "
    "Apply only after user review of each evidence reference."
)
_PROPOSAL_STRUCTURAL_HEADINGS = frozenset(
    {
        "## evidence-grounded summary",
        "## review boundary",
        "### problem observed",
        "### technology used",
        "### personal contribution",
        "### verification",
        "### outcome",
    }
)

_COMPREHENSIVE_EVAL_INPUTS = frozenset(
    {"instruction", "model", "dataset", "grader", "rubric"}
)


def required_eval_gate(changed_inputs: Set[EvalInput]) -> EvalGate:
    """Select the required eval tier for a closed set of changed inputs."""
    inputs = tuple(sorted(changed_inputs, key=lambda item: (item.kind, item.version, item.digest)))
    if not inputs:
        raise ValueError("changed eval inputs must not be empty")
    gate = (
        "comprehensive"
        if any(item.kind in _COMPREHENSIVE_EVAL_INPUTS for item in inputs)
        else "smoke"
    )
    return EvalGate(gate, inputs, 60 if gate == "comprehensive" else 12, 0)


def calibrate_human_rubric(
    gold: Sequence[GoldClaim],
    judgments: Sequence[Judgment],
    *,
    run_id: str,
) -> CalibrationResult:
    """Measure owner-adjudicated rubric agreement against gold claim labels."""
    gold_items = tuple(gold)
    judgment_items = tuple(judgments)
    if len(gold_items) != len(judgment_items):
        raise ValueError("gold claims and judgments must have equal counts")
    if len(gold_items) < 24:
        raise ValueError("calibration requires at least 24 adjudicated claims")
    if any(item.verdict not in {"pass", "fail"} for item in judgment_items):
        raise ValueError("calibration judgments must be pass or fail")

    predicted = tuple(
        claim.label
        if judgment.verdict == "pass"
        else ("unsupported" if claim.label == "supported" else "supported")
        for claim, judgment in zip(gold_items, judgment_items, strict=True)
    )
    count = len(gold_items)
    agreements = sum(
        judgment.verdict == "pass" for judgment in judgment_items
    )
    agreement = agreements / count
    unsupported_indexes = tuple(
        index for index, claim in enumerate(gold_items) if claim.label == "unsupported"
    )
    if not unsupported_indexes:
        raise ValueError("calibration requires unsupported gold claims")
    unsupported_recall = sum(
        judgment_items[index].verdict == "pass" for index in unsupported_indexes
    ) / len(unsupported_indexes)

    gold_supported = sum(claim.label == "supported" for claim in gold_items) / count
    predicted_supported = sum(label == "supported" for label in predicted) / count
    expected_agreement = (
        gold_supported * predicted_supported
        + (1 - gold_supported) * (1 - predicted_supported)
    )
    kappa = (
        1.0
        if agreement == 1.0 and expected_agreement == 1.0
        else (agreement - expected_agreement) / (1 - expected_agreement)
    )
    passed = agreement >= 0.95 and unsupported_recall == 1.0 and kappa >= 0.8
    return CalibrationResult(
        run_id,
        count,
        agreement,
        unsupported_recall,
        kappa,
        passed,
    )


def grade_portfolio_eval_output(
    case: EvalCase,
    output: PortfolioEvalOutput,
    gold: Sequence[GoldClaim],
) -> DeterministicGrade:
    """Grade declared facts, evidence references, and context without inference."""
    gold_by_id = {item.claim_id: item for item in gold}
    if len(gold_by_id) != len(gold):
        raise ValueError("duplicate gold claim_id")
    issues: set[str] = set()
    if output.case_id != case.case_id:
        issues.add("case-id-mismatch")
    if case.category == "unsupported-distractor" and output.claims:
        issues.add("unexpected-claim")

    emitted_supported_facts: set[str] = set()
    allowed_evidence = set(case.allowed_evidence_ids)
    forbidden = tuple(value.casefold() for value in case.forbidden_claims)
    for claim in output.claims:
        if claim.context_id != case.context_id:
            issues.add("context-mismatch")
        if set(claim.evidence_ids) - allowed_evidence:
            issues.add("invalid-evidence-id")
        if any(value in claim.text.casefold() for value in forbidden):
            issues.add("forbidden-claim")
        for fact_id in claim.fact_ids:
            gold_claim = gold_by_id.get(fact_id)
            if gold_claim is None:
                issues.add("unknown-fact-id")
                continue
            if gold_claim.label == "unsupported":
                issues.add("unsupported-fact-id")
                continue
            emitted_supported_facts.add(fact_id)
            if gold_claim.context_id != claim.context_id:
                issues.add("context-mismatch")
            if not set(gold_claim.evidence_ids).issubset(claim.evidence_ids):
                issues.add("fact-evidence-mismatch")
    if set(case.required_claim_ids) - emitted_supported_facts:
        issues.add("missing-required-fact")
    return DeterministicGrade(case.case_id, tuple(sorted(issues)))


def compare_eval_results(
    baseline: ComprehensiveEvalResult,
    current: ComprehensiveEvalResult,
) -> EvalComparison:
    """Compare correctness and resource axes without conflating their signals."""
    baseline_input = baseline.input_tokens
    baseline_output = baseline.output_tokens
    baseline_latency = baseline.p95_latency_seconds
    current_input = current.input_tokens
    current_output = current.output_tokens
    current_latency = current.p95_latency_seconds
    if (
        baseline_input is None
        or baseline_output is None
        or baseline_latency is None
        or min(baseline_input, baseline_output, baseline_latency) <= 0
    ):
        raise ValueError("baseline comparison metrics must be positive and available")
    if (
        current_input is None
        or current_output is None
        or current_latency is None
        or min(current_input, current_output, current_latency) < 0
    ):
        raise ValueError("current comparison metrics must be nonnegative and available")
    correctness_equal = (
        baseline.output_count,
        baseline.error_count,
        baseline.upper_error_bound,
        baseline.per_case_run_counts,
    ) == (
        current.output_count,
        current.error_count,
        current.upper_error_bound,
        current.per_case_run_counts,
    )
    return EvalComparison(
        correctness_equal,
        current_input / baseline_input - 1,
        current_output / baseline_output - 1,
        current_latency / baseline_latency - 1,
    )


def eval_activation_allowed(
    gate: EvalGate,
    receipt: EvalActivationReceipt,
    calibration: CalibrationResult,
    comprehensive_result: EvalResult | ComprehensiveEvalResult | None,
    *,
    result_digest: str | None,
    changed_at: datetime,
) -> bool:
    """Allow activation only for a fresh exact-input passing receipt."""
    if changed_at.tzinfo is None or changed_at.utcoffset() is None:
        raise ValueError("changed_at must be timezone-aware")
    if gate.gate == "comprehensive" and not isinstance(
        comprehensive_result, ComprehensiveEvalResult
    ):
        return False
    result_bound = (
        comprehensive_result is not None
        and comprehensive_result.passed
        and receipt.run_id == comprehensive_result.run_id
        and receipt.verdict == comprehensive_result.verdict
        and receipt.output_count == comprehensive_result.output_count
        and receipt.error_count == comprehensive_result.error_count
        and result_digest == receipt.result_digest
        and result_digest == eval_result_digest(comprehensive_result)
    )
    coverage_bound = not isinstance(comprehensive_result, ComprehensiveEvalResult) or (
        len(comprehensive_result.expected_case_ids) == 12
        and tuple(sorted(comprehensive_result.expected_case_ids))
        == tuple(sorted(case_id for case_id, _count in comprehensive_result.per_case_run_counts))
    )
    return (
        receipt.gate == gate.gate
        and receipt.verdict == "pass"
        and frozenset(receipt.inputs) == frozenset(gate.inputs)
        and receipt.completed_at > changed_at
        and receipt.output_count >= gate.minimum_runs
        and receipt.error_count <= gate.maximum_errors
        and (gate.gate != "comprehensive" or calibration.passed)
        and result_bound
        and coverage_bound
    )


def _proposal_authority_paths(root: Path) -> tuple[Path, Path]:
    """Return the required, root-contained authority files for proposal work."""
    root = root.resolve()
    paths = (root / "manifest.md", root / "project-mapping.md")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("proposal authority files must be regular files")
        if not path.resolve().is_relative_to(root):
            raise ValueError("proposal authority files must remain inside the research root")
    return paths


def _validate_proposal_text(
    proposed_text: str,
    evidence_ids: Sequence[str],
    original_text: str | None = None,
    *,
    review_path: Path | None = None,
) -> None:
    """Require every concrete narrative line to cite the proposal evidence set."""
    if "<!--" in proposed_text or "-->" in proposed_text:
        raise ValueError("proposal text must not contain HTML comments")
    references: set[str] = set()
    for match in _PROPOSAL_EVIDENCE_REFERENCE.finditer(proposed_text):
        values = [value.strip() for value in match.group(1).split(",")]
        if not values or any(not value for value in values):
            raise ValueError("proposal evidence reference must not be empty")
        references.update(values)
    allowed = set(evidence_ids)
    if not references and review_path is None:
        raise ValueError("proposal requires an inline evidence reference")
    if references - allowed:
        raise ValueError("proposal text references evidence outside its evidence_ids")
    structural_headings = set(_PROPOSAL_STRUCTURAL_HEADINGS)
    if original_text is not None:
        structural_headings.update(
            line.strip().lower()
            for line in original_text.splitlines()
            if line.startswith("# ")
        )
    review_boundary = False
    for line in proposed_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and stripped.lower() in structural_headings:
            review_boundary = stripped.lower() == "## review boundary"
            continue
        if review_boundary and stripped == _PROPOSAL_REVIEW_BOUNDARY_DISCLAIMER:
            continue
        if review_path is None and not _PROPOSAL_EVIDENCE_REFERENCE_AT_LINE_END.search(stripped):
            raise ValueError("proposal concrete claim requires an evidence reference")


def _proposal_review_path(root: Path, proposal_id: str, review_path: Path) -> Path:
    expected = Path("reviews") / f"{proposal_id}.md"
    if review_path != expected:
        raise ValueError("proposal review must use its canonical reviews path")
    return _safe_research_path(root, review_path)


def _validate_proposal_review(root: Path, proposal: NarrativeProposal) -> None:
    if proposal.review_path is None:
        return
    review = _proposal_review_path(root, proposal.proposal_id, proposal.review_path)
    if review.is_symlink() or not review.is_file():
        raise ValueError("proposal review must be a regular file")
    contents = review.read_text(encoding="utf-8")
    if sha256(contents.encode("utf-8")).hexdigest() != proposal.review_digest:
        raise ValueError("proposal review digest diverged")
    if "<!-- portfolio-review-only -->" not in contents:
        raise ValueError("proposal review must be marked review-only")
    if any(evidence_id not in contents for evidence_id in proposal.evidence_ids):
        raise ValueError("proposal review must map every evidence ID")


def _validate_proposal_revisions(
    root: Path,
    proposals: Sequence[NarrativeProposal],
) -> None:
    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    if len(by_id) != len(proposals):
        raise ValueError("duplicate proposal ID")
    successors: dict[str, NarrativeProposal] = {}
    for proposal in proposals:
        if proposal.supersedes is None:
            continue
        prior = by_id.get(proposal.supersedes)
        if prior is None or prior.state != "applied" or prior.source_path != proposal.source_path:
            raise ValueError("proposal supersedes must identify an applied proposal for the same source")
        if prior.proposed_text != proposal.original_text:
            raise ValueError("proposal revision original text must match superseded output")
        if prior.proposal_id in successors:
            raise ValueError("proposal supersedes already has a successor")
        successors[prior.proposal_id] = proposal
    for source_path in {proposal.source_path for proposal in proposals}:
        source = _proposal_source(root, source_path)
        source_proposals = [proposal for proposal in proposals if proposal.source_path == source_path]
        pending = [proposal for proposal in source_proposals if proposal.state in {"draft", "approved"}]
        if len(pending) > 1:
            raise ValueError("proposal source has multiple pending revisions")
        if pending:
            expected = pending[0].original_text
        else:
            leaves = [proposal for proposal in source_proposals if proposal.state == "applied" and proposal.proposal_id not in successors]
            if len(leaves) != 1:
                raise ValueError("proposal source must have exactly one applied leaf")
            expected = leaves[0].proposed_text
        if not source.is_file() or sha256(source.read_bytes()).hexdigest() != sha256(expected.encode("utf-8")).hexdigest():
            raise ValueError("proposal source bytes diverged")


def normalize_repositories(
    occurrences: Sequence[RawRepositoryOccurrence],
    observations: Sequence[RepositoryObservation],
) -> list[LogicalRepository]:
    occurrence_by_id = {item.occurrence_id: item for item in occurrences}
    groups: dict[tuple[str, ...], list[RepositoryObservation]] = {}
    for observation in observations:
        if observation.occurrence_id not in occurrence_by_id:
            raise ValueError("observation references unknown occurrence_id")
        key = repository_group_key(observation, observations)
        groups.setdefault(key, []).append(observation)

    logical_repositories: list[LogicalRepository] = []
    for key, members in sorted(groups.items()):
        occurrence_ids = tuple(sorted(item.occurrence_id for item in members))
        commits = tuple(sorted({commit for item in members for commit in item.reachable_commits}))
        refs = tuple(sorted({ref for item in members for ref in item.observed_refs}))
        period_ids = tuple(sorted({period for item in members for period in item.period_ids}))
        logical_id = "logical-" + sha256("|".join(key).encode()).hexdigest()[:16]
        logical_repositories.append(
            LogicalRepository(
                logical_id,
                occurrence_ids,
                members[0].context_id,
                period_ids,
                "unknown",
                commits[0] if commits else None,
                commits[-1] if commits else None,
                len(commits),
                "cataloged",
                "pending",
                "cataloged from bounded Git observations",
                None,
                refs,
                sha256("\n".join(commits).encode()).hexdigest(),
            )
        )
    return logical_repositories


def classify_commits(
    manifest: ResearchManifest,
    logical: LogicalRepository,
    commits: Sequence[CommitObservation],
) -> list[CommitDisposition]:
    contexts = {item.context_id: item for item in manifest.contexts}
    context = contexts.get(logical.context_id)
    if context is None:
        raise ValueError("logical repository references unknown context")
    period_by_id = {item.period_id: item for item in manifest.periods}
    periods = [period_by_id[item] for item in logical.period_ids]
    dispositions: list[CommitDisposition] = []
    for commit in commits:
        identity = next(
            (
                item
                for item in manifest.authors
                if item.name == commit.author_name and item.email == commit.author_email
            ),
            None,
        )
        if identity is None:
            continue
        authored_at = commit.authored_at.astimezone(timezone.utc)
        matching_period = next(
            (
                period
                for period in periods
                if period.contains(commit.authored_at.astimezone(period.starts_at.tzinfo))
            ),
            None,
        )
        if matching_period is None:
            continue
        dispositions.append(
            CommitDisposition(
                logical.logical_repository_id,
                commit.commit_id,
                identity.identity_id,
                authored_at,
                True,
                "yes" if commit.parent_count > 1 else "no",
                "unknown",
                "unknown",
                "unknown",
                "unassigned",
                (),
                None,
                "contribution",
            )
        )
    return dispositions


def classify_refresh(
    previous_refs: Sequence[str],
    current_refs: Sequence[str],
    prior_dispositions: Sequence[CommitDisposition],
    current_commits: Sequence[CommitObservation],
    *,
    mode: str = "auto",
    measurement_error: str | None = None,
    candidate_commit_ids: Set[str] | None = None,
) -> RefreshDecision:
    if measurement_error is not None:
        return RefreshDecision("blocked", f"Git measurement failed: {measurement_error}")
    if mode != "auto":
        return RefreshDecision("blocked", f"unknown refresh mode: {mode}")
    previous = _refs_by_name(previous_refs)
    current = _refs_by_name(current_refs)
    if previous is None or current is None:
        return RefreshDecision("blocked", "malformed observed refs")
    if set(previous) - set(current):
        return RefreshDecision("full-reconciliation", "an observed ref was removed")
    reachable = {item.commit_id for item in current_commits}
    commits_by_id = {item.commit_id: item for item in current_commits}
    for name, old_object_id in previous.items():
        new_object_id = current.get(name)
        if (
            new_object_id is not None
            and new_object_id != old_object_id
            and not _is_ancestor(old_object_id, new_object_id, commits_by_id)
        ):
            return RefreshDecision(
                "full-reconciliation",
                f"the prior object for {name} is not an ancestor of its current object",
            )
    disposed = {item.commit_id for item in prior_dispositions}
    candidates = reachable if candidate_commit_ids is None else set(candidate_commit_ids)
    candidate_count = len(candidates - disposed)
    return RefreshDecision("incremental", f"{candidate_count} candidate commits remain after prior dispositions")


def publish_transaction(
    writes: Sequence[PendingWrite],
    journal: Path,
    *,
    root: Path | None = None,
) -> PublicationResult:
    root = (journal.parent.parent if root is None else root).resolve()
    journal = Path(journal)
    if not journal.is_absolute():
        journal = (Path.cwd() / journal).absolute()
    if journal.is_symlink() or (journal.exists() and not journal.is_file()):
        raise ValueError("publication journal must be a regular file")
    try:
        journal_relative = journal.relative_to(root)
    except ValueError as exc:
        raise ValueError("publication journal escapes the research root") from exc
    _safe_research_path(root, journal_relative)
    if journal.exists():
        existing = load_publication_result(journal)
        if existing.state == "committed":
            if tuple(_write_contract(write) for write in existing.writes) != tuple(
                _write_contract(write) for write in writes
            ):
                raise ValueError("committed transaction does not match requested writes")
            if any(
                not (root / write.target).is_file()
                or (root / write.target).is_symlink()
                or sha256((root / write.target).read_bytes()).hexdigest()
                != write.output_digest
                for write in existing.writes
            ):
                raise ValueError("committed transaction output bytes diverged")
            return existing
        if existing.state in {"compensated", "manual-recovery"}:
            raise ValueError("a compensated or recovery transaction requires a new transaction ID")
        _reconcile_nonterminal_transaction(existing, root, journal)
        raise ValueError("reconciled transaction requires a new transaction ID")
    transaction_id = journal.stem
    for write in writes:
        target = root / write.target
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("publication target escapes the research root")
        if write.original_text is None:
            if target.exists() or target.is_symlink():
                raise ValueError("new publication target already exists")
        elif (
            target.is_symlink()
            or not target.is_file()
            or sha256(target.read_bytes()).hexdigest() != write.pre_state_digest
        ):
            raise ValueError("publication target no longer matches its pre-state")
    current = PublicationResult(transaction_id, "prepared", tuple(writes), journal_relative)
    atomic_write(journal, render_publication_result(current))
    completed: list[int] = []
    attempted: int | None = None
    try:
        mutable = list(current.writes)
        for index, write in enumerate(mutable):
            attempted = index
            target = root / write.target
            atomic_write(target, write.replacement_text)
            completed.append(index)
            mutable[index] = replace(write, state="replaced")
            state = "committed" if len(completed) == len(mutable) else "replacing"
            current = PublicationResult(transaction_id, state, tuple(mutable), journal_relative)
            atomic_write(journal, render_publication_result(current))
            attempted = None
        return current
    except BaseException:
        mutable = list(current.writes)
        if attempted is not None and attempted not in completed:
            attempted_write = mutable[attempted]
            attempted_target = root / attempted_write.target
            if (
                attempted_target.is_file()
                and sha256(attempted_target.read_bytes()).hexdigest() == attempted_write.output_digest
            ):
                completed.append(attempted)
        manual_recovery = False
        for index in reversed(completed):
            write = mutable[index]
            target = root / write.target
            try:
                if write.original_text is None:
                    if target.is_symlink():
                        raise OSError("published target became a symlink before compensation")
                    if target.exists() and (
                        not target.is_file()
                        or sha256(target.read_bytes()).hexdigest() != write.output_digest
                    ):
                        raise OSError("published target diverged before compensation")
                    target.unlink(missing_ok=True)
                else:
                    if (
                        target.is_symlink()
                        or not target.is_file()
                        or sha256(target.read_bytes()).hexdigest() != write.output_digest
                    ):
                        raise OSError("published target diverged before compensation")
                    atomic_write(target, write.original_text)
                mutable[index] = replace(write, state="compensated")
            except (OSError, ValueError):
                mutable[index] = replace(write, state="manual-recovery")
                manual_recovery = True
        state = "manual-recovery" if manual_recovery else "compensated"
        result = PublicationResult(transaction_id, state, tuple(mutable), journal_relative)
        atomic_write(journal, render_publication_result(result))
        return result


def _write_contract(write: PendingWrite) -> tuple[Path, str | None, str, str | None, str]:
    return (
        write.target,
        write.original_text,
        write.replacement_text,
        write.pre_state_digest,
        write.output_digest,
    )


def _reconcile_nonterminal_transaction(
    existing: PublicationResult,
    root: Path,
    journal: Path,
) -> PublicationResult:
    mutable = list(existing.writes)
    manual_recovery = False
    for index in reversed(range(len(mutable))):
        write = mutable[index]
        target = root / write.target
        try:
            if write.state == "replaced":
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or sha256(target.read_bytes()).hexdigest() != write.output_digest
                ):
                    raise OSError("replaced target diverged before reconciliation")
                if write.original_text is None:
                    target.unlink()
                else:
                    atomic_write(target, write.original_text)
                mutable[index] = replace(write, state="compensated")
            elif write.state == "compensated":
                if write.original_text is None:
                    if target.exists() or target.is_symlink():
                        raise OSError("compensated new target still exists")
                elif (
                    target.is_symlink()
                    or not target.is_file()
                    or sha256(target.read_bytes()).hexdigest() != write.pre_state_digest
                ):
                    raise OSError("compensated target diverged")
            elif write.state == "pending":
                if target.is_symlink():
                    raise OSError("pending target is a symlink")
                current_digest = (
                    sha256(target.read_bytes()).hexdigest()
                    if target.is_file()
                    else None
                )
                if current_digest == write.output_digest:
                    if write.original_text is None:
                        target.unlink()
                    else:
                        atomic_write(target, write.original_text)
                    mutable[index] = replace(write, state="compensated")
                elif current_digest == write.pre_state_digest:
                    if existing.state == "prepared":
                        mutable[index] = replace(write, state="compensated")
                elif write.original_text is None and current_digest is None:
                    if existing.state == "prepared":
                        mutable[index] = replace(write, state="compensated")
                else:
                    raise OSError("pending target diverged from known pre-state and output")
        except (OSError, ValueError):
            mutable[index] = replace(write, state="manual-recovery")
            manual_recovery = True
    state = "manual-recovery" if manual_recovery else "compensated"
    result = PublicationResult(
        existing.transaction_id,
        state,
        tuple(mutable),
        existing.journal_path,
    )
    atomic_write(journal, render_publication_result(result))
    if manual_recovery:
        raise ValueError("nonterminal transaction requires manual recovery")
    return result


def build_index(
    catalog: Sequence[LogicalRepository],
    ledgers: Sequence[EvidenceRecord],
    narrative_sources: Mapping[str, str] | None = None,
) -> str:
    """Render a deterministic, reference-only index for complete evidence ledgers."""
    del catalog
    narrative_sources = {} if narrative_sources is None else dict(narrative_sources)
    if any(ledger.state != "complete" for ledger in ledgers):
        raise ValueError("index requires complete evidence ledgers")
    rows = sorted(
        (
            ledger.context_id,
            ", ".join(ledger.period_ids),
            ledger.project_id,
            ledger.technology_use,
            ledger.problem,
            ledger.decision_type,
            ledger.evidence_id,
            narrative_sources.get(ledger.project_id, ""),
        )
        for ledger in ledgers
    )
    lines = [
        "# Portfolio evidence index",
        "",
        "| Context | Period | Project | Technology | Problem | Decision type | Evidence ID | Narrative source |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {' | '.join(_index_cell(value) for value in row)} |"
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _index_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def prepare_proposal(
    root: Path,
    proposal_id: str,
    source_path: Path,
    proposed_text: str,
    evidence_ids: Sequence[str],
    *,
    review_path: Path | None = None,
    supersedes: str | None = None,
) -> NarrativeProposal:
    root = root.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", proposal_id):
        raise ValueError("proposal_id must be a safe identifier")
    _proposal_authority_paths(root)
    source = _proposal_source(root, source_path)
    if not source.is_file() or source.is_symlink():
        raise ValueError("proposal source must be a regular file")
    original_text = source.read_text(encoding="utf-8")
    _validate_proposal_evidence(root, evidence_ids, source_path)
    _validate_proposal_text(proposed_text, evidence_ids, original_text, review_path=review_path)
    proposal_root = root / "proposals"
    if proposal_root.exists() or proposal_root.is_symlink():
        existing = {proposal.proposal_id: proposal for proposal in (load_proposal(path) for path in _proposal_paths(root))}
    else:
        existing = {}
    if supersedes is not None:
        prior = existing.get(supersedes)
        if prior is None or prior.state != "applied" or prior.source_path != source_path:
            raise ValueError("proposal supersedes must identify an applied proposal for the same source")
        if prior.proposed_text != original_text:
            raise ValueError("proposal superseded output must match the current source")
        if any(item.supersedes == supersedes and item.proposal_id != proposal_id for item in existing.values()):
            raise ValueError("proposal supersedes already has a successor")
    elif any(item.source_path == source_path and item.state == "applied" for item in existing.values()):
        raise ValueError("proposal revision must supersede the current applied proposal")
    snapshot_path = Path("snapshots") / proposal_id / source_path
    _validate_snapshot_path(root, proposal_id, snapshot_path)
    snapshot = _safe_research_path(root, snapshot_path)
    if snapshot.exists():
        if snapshot.is_symlink() or not snapshot.is_file():
            raise ValueError("proposal snapshot is not a regular file")
        if snapshot.read_text(encoding="utf-8") != original_text:
            raise ValueError("proposal snapshot already exists with different bytes")
    else:
        atomic_write(snapshot, original_text)
    digest = sha256(original_text.encode("utf-8")).hexdigest()
    review_digest = None
    if review_path is not None:
        review = _proposal_review_path(root, proposal_id, review_path)
        if review.is_symlink() or not review.is_file():
            raise ValueError("proposal review must be a regular file")
        review_digest = sha256(review.read_bytes()).hexdigest()
    proposal = NarrativeProposal(
        proposal_id,
        source_path,
        digest,
        snapshot_path,
        digest,
        original_text,
        proposed_text,
        tuple(evidence_ids),
        "draft",
        None,
        None,
        None,
        review_path,
        review_digest,
        supersedes,
    )
    proposal_path = _safe_research_path(root, Path("proposals") / f"{proposal_id}.md")
    if proposal_path.exists():
        if proposal_path.is_symlink() or not proposal_path.is_file():
            raise ValueError("proposal already exists and is not a regular file")
        stored = load_proposal(proposal_path)
        if stored != proposal:
            raise ValueError("proposal already exists with different bytes")
        return stored
    proposal_text = render_proposal(proposal)
    try:
        atomic_write(proposal_path, proposal_text)
    except BaseException:
        _compensate_partial_proposal_record(source, digest, snapshot, proposal_path, proposal_text)
        raise
    return proposal


def _compensate_partial_proposal_record(
    source: Path,
    source_digest: str,
    snapshot: Path,
    proposal_path: Path,
    proposal_text: str,
) -> None:
    """Remove partial proposal artifacts only when the source pre-state still matches."""
    if (
        source.is_symlink()
        or not source.is_file()
        or sha256(source.read_bytes()).hexdigest() != source_digest
        or snapshot.is_symlink()
        or not snapshot.is_file()
        or sha256(snapshot.read_bytes()).hexdigest() != source_digest
        or proposal_path.is_symlink()
        or not proposal_path.is_file()
        or sha256(proposal_path.read_bytes()).hexdigest() != sha256(proposal_text.encode("utf-8")).hexdigest()
    ):
        return
    proposal_path.unlink()
    snapshot.unlink()


def supersede_draft(root: Path, proposal: NarrativeProposal) -> NarrativeProposal:
    """Retain a persisted, unapplied draft before a replacement proposal."""
    if proposal.state != "draft":
        raise ValueError("only a draft proposal can be superseded")
    root = root.resolve()
    proposal_path = _safe_research_path(root, Path("proposals") / f"{proposal.proposal_id}.md")
    if proposal_path.is_symlink() or not proposal_path.is_file():
        raise ValueError("proposal record is missing")
    if load_proposal(proposal_path) != proposal:
        raise ValueError("proposal record does not match supplied draft")
    superseded = replace(proposal, state="superseded")
    atomic_write(proposal_path, render_proposal(superseded))
    return superseded


def apply_proposal(root: Path, proposal: NarrativeProposal) -> NarrativeProposal:
    applied = apply_proposals(root, (proposal,))
    return applied[0]


def apply_proposals(
    root: Path,
    proposals: Sequence[NarrativeProposal],
) -> tuple[NarrativeProposal, ...]:
    """Apply persisted, approved narrative proposals as one recoverable transaction."""
    if not proposals:
        raise ValueError("proposal batch must not be empty")
    proposal_ids = tuple(proposal.proposal_id for proposal in proposals)
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("proposal batch contains duplicate proposal IDs")
    root = root.resolve()
    _proposal_authority_paths(root)
    persisted: list[tuple[NarrativeProposal, Path, Path, str]] = []
    for proposal in proposals:
        if proposal.state not in {"approved", "applied"}:
            raise ValueError("proposal must be approved before apply")
        _validate_proposal_evidence(root, proposal.evidence_ids, proposal.source_path)
        _validate_proposal_text(
            proposal.proposed_text,
            proposal.evidence_ids,
            proposal.original_text,
            review_path=proposal.review_path,
        )
        _validate_proposal_review(root, proposal)
        proposal_path = _safe_research_path(
            root,
            Path("proposals") / f"{proposal.proposal_id}.md",
        )
        if proposal_path.is_symlink() or not proposal_path.is_file():
            raise ValueError("proposal record is missing")
        stored = load_proposal(proposal_path)
        if stored != proposal:
            raise ValueError("proposal record does not match supplied proposal")
        source = _proposal_source(root, proposal.source_path)
        _validate_snapshot_path(root, proposal.proposal_id, proposal.snapshot_path)
        snapshot = _safe_research_path(root, proposal.snapshot_path)
        if not snapshot.is_file() or snapshot.is_symlink():
            raise ValueError("proposal snapshot is missing")
        if sha256(snapshot.read_bytes()).hexdigest() != proposal.snapshot_digest:
            raise ValueError("proposal snapshot digest diverged")
        if proposal.state == "applied":
            applied_digest = proposal.applied_output_digest
            if applied_digest is None:
                raise ValueError("applied proposal output digest is missing")
            if not source.is_file() or source.is_symlink() or sha256(source.read_bytes()).hexdigest() != applied_digest:
                raise ValueError("applied proposal output diverged")
        elif proposal.state == "approved":
            if not source.is_file() or source.is_symlink():
                raise ValueError("proposal source must be a regular file")
            if sha256(source.read_bytes()).hexdigest() != proposal.source_digest:
                raise ValueError("proposal source digest diverged")
        persisted.append((proposal, proposal_path, source, proposal_path.read_text(encoding="utf-8")))

    if all(proposal.state == "applied" for proposal, *_ in persisted):
        return tuple(proposal for proposal, *_ in persisted)
    if any(proposal.state != "approved" for proposal, *_ in persisted):
        raise ValueError("proposal batch cannot mix applied and approved states")
    source_paths = tuple(source for _, _, source, _ in persisted)
    if len(set(source_paths)) != len(source_paths):
        raise ValueError("proposal batch contains duplicate source paths")

    private_root = root.parent.resolve()
    transaction_id = "proposal-" + "-".join(proposal.proposal_id for proposal, *_ in persisted)
    writes: list[PendingWrite] = []
    applied_records: list[NarrativeProposal] = []
    for proposal, proposal_path, source, proposal_record in persisted:
        applied_digest = sha256(proposal.proposed_text.encode("utf-8")).hexdigest()
        applied = replace(
            proposal,
            state="applied",
            applied_output_digest=applied_digest,
        )
        applied_records.append(applied)
        writes.extend(
            (
                PendingWrite(
                    source.relative_to(private_root),
                    proposal.original_text,
                    proposal.proposed_text,
                    proposal.source_digest,
                    applied_digest,
                ),
                PendingWrite(
                    Path(root.name) / proposal_path.relative_to(root),
                    proposal_record,
                    render_proposal(applied),
                    sha256(proposal_record.encode("utf-8")).hexdigest(),
                    sha256(render_proposal(applied).encode("utf-8")).hexdigest(),
                ),
            )
        )
    journal = _next_proposal_journal(root, transaction_id, tuple(writes))
    result = publish_transaction(writes, journal, root=private_root)
    if result.state != "committed":
        raise ValueError(f"proposal batch {result.state}")
    return tuple(
        load_proposal(_safe_research_path(root, Path("proposals") / f"{proposal.proposal_id}.md"))
        for proposal in applied_records
    )


def _proposal_source(root: Path, source_path: Path) -> Path:
    if source_path.is_absolute() or ".." in source_path.parts:
        raise ValueError("proposal source path must be private-root relative")
    parts = source_path.parts
    if not (
        (len(parts) >= 3 and parts[:2] == ("profile", "portfolios"))
        or (len(parts) >= 4 and parts[0] == "companies" and parts[2] == "portfolios")
    ):
        raise ValueError("proposal source must be a narrative source")
    source_root = root.parent.resolve()
    cursor = source_root
    for part in source_path.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("proposal source parent must not be a symlink")
    lexical_source = source_root / source_path
    if lexical_source.is_symlink():
        raise ValueError("proposal source must not be a symlink")
    source = lexical_source.resolve()
    if not source.is_relative_to(source_root):
        raise ValueError("proposal source escapes private root")
    resolved_parts = source.relative_to(source_root).parts
    if not (
        (len(resolved_parts) >= 3 and resolved_parts[:2] == ("profile", "portfolios"))
        or (len(resolved_parts) >= 4 and resolved_parts[0] == "companies" and resolved_parts[2] == "portfolios")
    ):
        raise ValueError("proposal source must be a narrative source")
    return source


def _safe_research_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("research path must be private-root relative")
    root = root.resolve()
    cursor = root
    for part in relative.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("research path parent must not be a symlink")
        if cursor.exists() and not cursor.is_dir():
            raise ValueError("research path parent must be a directory")
    parent = (root / relative).parent.resolve()
    if not parent.is_relative_to(root):
        raise ValueError("research path escapes private root")
    return root / relative


def _validate_snapshot_path(root: Path, proposal_id: str, snapshot_path: Path) -> None:
    expected_prefix = Path("snapshots") / proposal_id
    try:
        snapshot_path.relative_to(expected_prefix)
    except ValueError as exc:
        raise ValueError("proposal snapshot must be inside its proposal directory") from exc


def _load_narrative_sources(path: Path, *, root: Path | None = None) -> dict[str, str]:
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = _split_index_row(line)
        if len(cells) != 7 and len(cells) != 8:
            raise ValueError("project mapping row must have a fixed shape")
        if cells[0] == "Context" or all(cell == "---" for cell in cells):
            continue
        if len(cells) == 7:
            project_id, source, state = cells[1], cells[3], cells[5]
        else:
            project_id, source, state = cells[2], cells[7], cells[6]
        if state not in {"confirmed", "candidate", "unmapped"}:
            raise ValueError("project mapping contains an unknown state")
        if state != "confirmed":
            continue
        if project_id and source:
            relative_source = Path(source)
            if relative_source.is_absolute() or ".." in relative_source.parts:
                raise ValueError("project mapping narrative source must be relative")
            if root is not None:
                validated_source = _proposal_source(root, relative_source)
                if not validated_source.is_file():
                    raise ValueError("project mapping narrative source is missing")
            mapping[project_id] = source
    return mapping


def _split_index_row(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line[1:-1]:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _narrative_context(manifest: ResearchManifest, source_path: Path) -> str:
    matches: list[str] = []
    for context in manifest.contexts:
        try:
            source_path.relative_to(context.narrative_root)
        except ValueError:
            continue
        matches.append(context.context_id)
    if len(matches) != 1:
        raise ValueError("proposal source must map to exactly one manifest context")
    return matches[0]


def _evidence_paths(root: Path) -> list[Path]:
    evidence_root = root / "evidence"
    if evidence_root.is_symlink() or not evidence_root.is_dir():
        raise ValueError("evidence root must be a regular directory")
    resolved_root = evidence_root.resolve()
    paths = sorted(evidence_root.glob("*.md"))
    for path in paths:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(resolved_root):
            raise ValueError("evidence files must be regular files inside the evidence root")
    return paths


def _proposal_paths(root: Path) -> list[Path]:
    proposal_root = root / "proposals"
    if proposal_root.is_symlink() or not proposal_root.is_dir():
        raise ValueError("proposal root must be a regular directory")
    resolved_root = proposal_root.resolve()
    paths = sorted(proposal_root.glob("*.md"))
    for path in paths:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(resolved_root):
            raise ValueError("proposal files must be regular files inside the proposal root")
    return paths


def _validate_proposal_evidence(
    root: Path,
    evidence_ids: Sequence[str],
    source_path: Path | None = None,
) -> None:
    known = {
        load_evidence(path).evidence_id
        for path in _evidence_paths(root)
    }
    if not evidence_ids or len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("proposal evidence IDs must be unique and nonempty")
    if set(evidence_ids) - known:
        raise ValueError("proposal references unknown evidence ID")
    if source_path is None:
        return
    manifest_path, mapping_path = _proposal_authority_paths(root)
    manifest = load_manifest(manifest_path)
    source_context = _narrative_context(manifest, source_path)
    mapping = _load_narrative_sources(mapping_path, root=root)
    records = {
        record.evidence_id: record
        for record in (load_evidence(path) for path in _evidence_paths(root))
    }
    for evidence_id in evidence_ids:
        record = records[evidence_id]
        if record.context_id != source_context:
            raise ValueError("proposal evidence context does not match narrative source")
        mapped_source = mapping.get(record.project_id)
        if mapped_source != source_path.as_posix():
            raise ValueError("proposal evidence project is not mapped to the narrative source")


def _next_proposal_journal(
    root: Path,
    transaction_id: str,
    expected_writes: Sequence[PendingWrite],
) -> Path:
    """Reconcile prior proposal journals before returning a fresh transaction path."""
    root = root.resolve()
    base = _safe_research_path(root, Path("transactions") / f"{transaction_id}.md")
    if base.is_symlink() or (base.exists() and not base.is_file()):
        raise ValueError("proposal transaction journal must be a regular file")
    retry_paths = _proposal_retry_paths(root, transaction_id)
    if retry_paths and not base.exists():
        raise ValueError("proposal retry journal sequence has a gap")
    if retry_paths and set(retry_paths) != set(range(1, max(retry_paths) + 1)):
        raise ValueError("proposal retry journal sequence has a gap")
    candidate = base
    attempt = 0
    while True:
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ValueError("proposal retry journal must be a regular file")
        if not candidate.exists():
            return candidate
        existing = load_publication_result(candidate)
        _validate_proposal_journal_contract(root, candidate, existing, expected_writes)
        if existing.state in {"prepared", "replacing", "compensating"}:
            _reconcile_nonterminal_transaction(existing, root.parent.resolve(), candidate)
        elif existing.state == "manual-recovery":
            raise ValueError("proposal retry journal requires manual recovery")
        elif existing.state == "committed":
            raise ValueError("committed proposal retry journal conflicts with requested batch")
        attempt += 1
        candidate = _safe_research_path(
            root,
            Path("transactions") / f"{transaction_id}-retry-{attempt}.md",
        )


def _proposal_retry_paths(root: Path, transaction_id: str) -> set[int]:
    transactions = _safe_research_path(root, Path("transactions"))
    if not transactions.exists():
        return set()
    if transactions.is_symlink() or not transactions.is_dir():
        raise ValueError("proposal transaction directory must be a regular directory")
    pattern = re.compile(rf"{re.escape(transaction_id)}-retry-([1-9][0-9]*)\.md")
    retries: set[int] = set()
    for path in transactions.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("proposal retry journal must be a regular file")
        retries.add(int(match.group(1)))
    return retries


def _validate_proposal_journal_contract(
    root: Path,
    journal: Path,
    existing: PublicationResult,
    expected_writes: Sequence[PendingWrite],
) -> None:
    expected_path = Path(root.name) / journal.relative_to(root)
    if (
        existing.transaction_id != journal.stem
        or existing.journal_path != expected_path
        or tuple(_write_contract(write) for write in existing.writes)
        != tuple(_write_contract(write) for write in expected_writes)
    ):
        raise ValueError("proposal retry journal does not match requested proposal batch")


def validate_research(root: Path, checks: Sequence[str]) -> list[ValidationIssue]:
    allowed = {
        "discovery",
        "logical-repositories",
        "attribution",
        "periods",
        "refresh",
        "lane",
        "dispositions",
        "evidence-shape",
        "privacy",
        "proposals",
        "references",
        "contexts",
    }
    selected_lane, requested_checks, request_error = _parse_validation_request(checks)
    if request_error is not None:
        return [ValidationIssue("error", "unknown-check", request_error)]
    issues: list[ValidationIssue] = []
    for check in requested_checks:
        if check not in allowed:
            issues.append(ValidationIssue("error", "unknown-check", f"unknown validation check: {check}"))
    if issues:
        return issues
    u7_checks = {"proposals", "references", "contexts"}
    if u7_checks.intersection(requested_checks):
        if selected_lane is not None:
            return [
                ValidationIssue(
                    "error",
                    "unknown-check",
                    "U7 validation does not accept a lane selector",
                )
            ]
        return _validate_u7_research(root, requested_checks)
    lane_checks = {"lane", "dispositions", "evidence-shape", "privacy"}
    if lane_checks.intersection(requested_checks):
        if selected_lane is None:
            return [
                ValidationIssue(
                    "error",
                    "unknown-check",
                    "lane validation requires exactly one lane selector",
                )
            ]
        return _validate_lane_research(root, requested_checks, selected_lane)
    required = {
        "discovery": root / "catalog.md",
        "logical-repositories": root / "catalog.md",
        "attribution": root / "manifest.md",
        "periods": root / "manifest.md",
        "refresh": root / "catalog.md",
    }
    for check in requested_checks:
        if not required[check].is_file():
            issues.append(ValidationIssue("error", "missing-artifact", f"required artifact is missing for {check}"))
    lane_paths = [root / "lanes" / f"lane-{index}.md" for index in range(1, 5)]
    for path in lane_paths:
        if not path.is_file():
            issues.append(ValidationIssue("error", "invalid-artifact", f"missing lane file: {path.name}"))
    if issues:
        return issues
    journals: list[PublicationResult] = []
    try:
        journals = [
            load_publication_result(path)
            for path in sorted((root / "transactions").glob("*.md"))
        ]
        owned_targets = [
            root / "manifest.md",
            root / "catalog.md",
            *lane_paths,
            *sorted((root / "dispositions").glob("*.md")),
        ]
        for target in owned_targets:
            ensure_readable(target, journals)
    except (OSError, ValueError) as exc:
        return [ValidationIssue("error", "blocking-journal", str(exc))]
    try:
        manifest = load_manifest(root / "manifest.md", journals=journals)
        occurrences, logical = load_catalog(root / "catalog.md", journals=journals)
        dispositions = [
            item
            for path in sorted((root / "dispositions").glob("*.md"))
            for item in load_dispositions(path, journals=journals)
        ]
        lanes = [_load_lane(path, journals) for path in lane_paths]
        validate_period_references(manifest, logical, ())
        logical_ids = {item.logical_repository_id for item in logical}
        if any(item.logical_repository_id not in logical_ids for item in dispositions):
            raise ValueError("disposition references unknown logical repository")
        disposition_keys = [
            (item.logical_repository_id, item.commit_id) for item in dispositions
        ]
        if len(disposition_keys) != len(set(disposition_keys)):
            raise ValueError("duplicate disposition across files")
        lane_members = [logical_id for _lane_id, members, _status in lanes for logical_id in members]
        if len(lane_members) != len(set(lane_members)) or set(lane_members) != logical_ids:
            raise ValueError("lane membership must cover every logical repository exactly once")
    except (OSError, ValueError) as exc:
        issues.append(ValidationIssue("error", "invalid-artifact", str(exc)))
        return issues

    try:
        context_roots = {
            discovery_root: (context.context_id, context.period_ids)
            for context in manifest.contexts
            for discovery_root in context.discovery_roots
        }
        inspector = GitPortfolioInspector(context_roots=context_roots)
        current_occurrences = _discover_approved_occurrences(
            inspector,
            tuple(context_roots),
        )
        current_observations = [
            inspector.observe(item)
            for item in current_occurrences
            if item.resolution == "resolved"
        ]
        current_logical = normalize_repositories(current_occurrences, current_observations)
        _validate_live_inventory(
            manifest,
            occurrences,
            logical,
            dispositions,
            current_occurrences,
            current_logical,
            inspector,
        )
    except GitPortfolioIssue as exc:
        issues.append(ValidationIssue("error", "git-measurement", str(exc)))
    except (OSError, ValueError) as exc:
        issues.append(ValidationIssue("error", "live-mismatch", str(exc)))
    return issues


def _validate_u7_research(root: Path, checks: Sequence[str]) -> list[ValidationIssue]:
    required = {
        "proposals": root / "proposals",
        "references": root / "index.md",
        "contexts": root / "manifest.md",
        "evidence-shape": root / "evidence",
        "privacy": root / "evidence",
        "mapping": root / "project-mapping.md",
        "catalog": root / "catalog.md",
    }
    directory_checks = {"proposals", "evidence-shape", "privacy"}
    missing = [
        check
        for check, path in required.items()
        if (
            path.is_symlink()
            or not (path.is_dir() if check in directory_checks else path.is_file())
        )
        and (check in checks or check in {"mapping", "catalog"})
    ]
    if missing:
        return [ValidationIssue("error", "missing-artifact", f"required artifact is missing for {missing[0]}")]
    try:
        manifest = load_manifest(root / "manifest.md")
        _occurrences, logical = load_catalog(root / "catalog.md")
        evidence = [load_evidence(path) for path in _evidence_paths(root)]
        known_evidence = {item.evidence_id for item in evidence}
        if len(known_evidence) != len(evidence):
            raise ValueError("duplicate evidence ID")
        validate_period_references(manifest, logical, evidence)
        if "privacy" in checks:
            _validate_evidence_privacy(evidence)
        narrative_sources = _load_narrative_sources(root / "project-mapping.md", root=root)
        expected_index = build_index(logical, evidence, narrative_sources)
        if root.joinpath("index.md").read_text(encoding="utf-8") != expected_index:
            raise ValueError("index does not match complete evidence ledgers")
        if "proposals" in checks:
            proposals = [load_proposal(path) for path in _proposal_paths(root)]
            for path, proposal in zip(_proposal_paths(root), proposals, strict=True):
                if proposal.proposal_id != path.stem:
                    raise ValueError("proposal filename must match proposal_id")
                if set(proposal.evidence_ids) - known_evidence:
                    raise ValueError("proposal references unknown evidence ID")
                _validate_proposal_evidence(root, proposal.evidence_ids, proposal.source_path)
                _validate_proposal_text(
                    proposal.proposed_text,
                    proposal.evidence_ids,
                    proposal.original_text,
                    review_path=proposal.review_path,
                )
                _validate_proposal_review(root, proposal)
                if "contexts" in checks:
                    _narrative_context(manifest, proposal.source_path)
                _validate_snapshot_path(root, proposal.proposal_id, proposal.snapshot_path)
                snapshot = _safe_research_path(root, proposal.snapshot_path)
                if not snapshot.is_file() or sha256(snapshot.read_bytes()).hexdigest() != proposal.snapshot_digest:
                    raise ValueError("proposal snapshot digest diverged")
            _validate_proposal_revisions(root, proposals)
    except (OSError, ValueError) as exc:
        return [ValidationIssue("error", "invalid-artifact", str(exc))]
    return []


def _parse_validation_request(
    checks: Sequence[str],
) -> tuple[str | None, tuple[str, ...], str | None]:
    materialized = tuple(checks)
    lane_positions = [index for index, check in enumerate(materialized) if check == "lane"]
    if not lane_positions:
        return None, materialized, None
    if len(lane_positions) != 1:
        return None, materialized, "lane validation accepts one lane check"
    position = lane_positions[0]
    if position + 1 >= len(materialized):
        return None, materialized, "lane validation requires exactly one lane selector"
    selected_lane = materialized[position + 1]
    if selected_lane not in {f"lane-{index}" for index in range(1, 5)}:
        return None, materialized, f"unknown lane selector: {selected_lane}"
    remaining = materialized[: position + 1] + materialized[position + 2 :]
    if any(item.startswith("lane-") for item in remaining):
        return None, materialized, "lane validation accepts exactly one lane selector"
    return selected_lane, remaining, None


def _validate_lane_research(
    root: Path,
    checks: Sequence[str],
    selected_lane_id: str,
) -> list[ValidationIssue]:
    lane_paths = [root / "lanes" / f"lane-{index}.md" for index in range(1, 5)]
    selected_path = root / "lanes" / f"{selected_lane_id}.md"
    required = (root / "manifest.md", root / "catalog.md")
    journals: list[PublicationResult]
    evidence_paths = sorted((root / "evidence").rglob("*.md"))
    disposition_paths = sorted((root / "dispositions").glob("*.md"))
    try:
        journals = [
            load_publication_result(path)
            for path in sorted((root / "transactions").glob("*.md"))
        ]
        for target in (
            *required,
            *lane_paths,
            *disposition_paths,
            *evidence_paths,
        ):
            ensure_readable(target, journals)
    except (OSError, ValueError) as exc:
        return [ValidationIssue("error", "blocking-journal", str(exc))]
    if not selected_path.is_file():
        return [
            ValidationIssue(
                "error",
                "missing-artifact",
                f"missing lane file: {selected_path.name}",
            )
        ]
    missing = next((path for path in required if not path.is_file()), None)
    if missing is not None:
        return [
            ValidationIssue(
                "error",
                "missing-artifact",
                f"required artifact is missing: {missing.name}",
            )
        ]
    try:
        manifest = load_manifest(root / "manifest.md", journals=journals)
        occurrences, logical = load_catalog(root / "catalog.md", journals=journals)
        lanes = [_load_lane(path, journals) for path in lane_paths if path.is_file()]
        selected = next((lane for lane in lanes if lane[0] == selected_lane_id), None)
        if selected is None:
            raise ValueError(f"missing lane file: {selected_path.name}")
        _lane_id, selected_members, selected_status = selected
        if selected_status != "complete":
            raise ValueError(f"{selected_lane_id} status must be complete")
        logical_ids = {item.logical_repository_id for item in logical}
        if set(selected_members) - logical_ids:
            raise ValueError("selected lane references unknown logical repository")
        all_members = [member for _lane, members, _status in lanes for member in members]
        if any(all_members.count(member) != 1 for member in selected_members):
            raise ValueError("selected lane member appears in another lane")
        dispositions = [
            item
            for path in disposition_paths
            for item in load_dispositions(path, journals=journals)
        ]
        disposition_keys = [
            (item.logical_repository_id, item.commit_id) for item in dispositions
        ]
        if len(disposition_keys) != len(set(disposition_keys)):
            raise ValueError("duplicate disposition across files")
        if any(item.logical_repository_id not in logical_ids for item in dispositions):
            raise ValueError("disposition references unknown logical repository")
        evidence = [load_evidence(path, journals=journals) for path in evidence_paths]
        evidence_ids = [item.evidence_id for item in evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate evidence_id")
        selected_evidence = [
            item
            for item in evidence
            if set(item.logical_repository_ids).intersection(selected_members)
        ]
        selected_dispositions = [
            item for item in dispositions if item.logical_repository_id in selected_members
        ]
        if "dispositions" in checks:
            selected_logical = [
                item
                for item in logical
                if item.logical_repository_id in selected_members
            ]
            _validate_selected_dispositions(
                manifest,
                occurrences,
                selected_logical,
                selected_dispositions,
                selected_evidence,
            )
        if "evidence-shape" in checks:
            _validate_selected_evidence(
                manifest,
                logical,
                selected_members,
                dispositions,
                selected_dispositions,
                selected_evidence,
            )
        if "privacy" in checks:
            _validate_evidence_privacy(selected_evidence)
    except GitPortfolioIssue as exc:
        return [ValidationIssue("error", "git-measurement", str(exc))]
    except (OSError, ValueError) as exc:
        code = "privacy" if str(exc).startswith("privacy:") else "invalid-artifact"
        message = str(exc).removeprefix("privacy: ")
        return [ValidationIssue("error", code, message)]
    return []


def _validate_selected_dispositions(
    manifest: ResearchManifest,
    occurrences: Sequence[RawRepositoryOccurrence],
    logical: Sequence[LogicalRepository],
    dispositions: Sequence[CommitDisposition],
    evidence: Sequence[EvidenceRecord],
) -> None:
    disposition_counts = {
        item.logical_repository_id: sum(
            disposition.logical_repository_id == item.logical_repository_id
            for disposition in dispositions
        )
        for item in logical
    }
    if any(
        disposition_counts[item.logical_repository_id]
        != item.qualifying_commit_count
        for item in logical
    ):
        raise ValueError("selected lane disposition count differs from qualifying commit count")
    _validate_selected_live_dispositions(
        manifest,
        occurrences,
        logical,
        dispositions,
    )
    if any(item.state == "unassigned" for item in dispositions):
        raise ValueError("selected lane contains unassigned dispositions")
    unresolved = [
        (item.logical_repository_id, item.commit_id, work_unit_id)
        for item in dispositions
        if item.state == "work-unit"
        for work_unit_id in item.work_unit_ids
        if not any(
            record.work_unit_id == work_unit_id
            and any(
                link.logical_repository_id == item.logical_repository_id
                and link.commit_id == item.commit_id
                for link in record.links
            )
            for record in evidence
        )
    ]
    if unresolved:
        raise ValueError(
            "selected lane has work-unit dispositions without evidence for the exact disposition"
        )


def _validate_selected_live_dispositions(
    manifest: ResearchManifest,
    occurrences: Sequence[RawRepositoryOccurrence],
    logical: Sequence[LogicalRepository],
    dispositions: Sequence[CommitDisposition],
) -> None:
    context_roots = {
        discovery_root: (context.context_id, context.period_ids)
        for context in manifest.contexts
        for discovery_root in context.discovery_roots
    }
    accepted_by_id = {item.occurrence_id: item for item in occurrences}
    selected_occurrence_ids = {
        occurrence_id
        for item in logical
        for occurrence_id in item.occurrence_ids
    }
    selected_occurrences = [
        accepted_by_id[occurrence_id]
        for occurrence_id in selected_occurrence_ids
        if occurrence_id in accepted_by_id
    ]
    if len(selected_occurrences) != len(selected_occurrence_ids):
        raise ValueError("selected lane references an unknown catalog occurrence")
    inspector = GitPortfolioInspector(context_roots=context_roots)
    current_occurrences = _discover_approved_occurrences(
        inspector,
        tuple(item.local_path for item in selected_occurrences),
    )
    current_occurrence_ids = [item.occurrence_id for item in current_occurrences]
    if any(occurrence_id not in accepted_by_id for occurrence_id in current_occurrence_ids):
        raise ValueError("selected lane discovery found an unknown catalog occurrence")
    if any(
        current_occurrence_ids.count(occurrence_id) != 1
        for occurrence_id in selected_occurrence_ids
    ):
        raise ValueError("selected lane occurrence set differs from catalog state")
    current_by_id = {item.occurrence_id: item for item in current_occurrences}
    for occurrence_id in selected_occurrence_ids:
        accepted = accepted_by_id.get(occurrence_id)
        current = current_by_id.get(occurrence_id)
        if (
            accepted is None
            or current is None
            or _selected_occurrence_state(current)
            != _selected_occurrence_state(accepted)
        ):
            raise ValueError("selected lane occurrence differs from catalog state")

    actual_by_logical: dict[str, list[CommitDisposition]] = {}
    for disposition in dispositions:
        actual_by_logical.setdefault(
            disposition.logical_repository_id,
            [],
        ).append(disposition)
    for item in logical:
        live_commits = inspector.commits(item)
        live_commit_ids = tuple(sorted({commit.commit_id for commit in live_commits}))
        live_commit_set_digest = sha256("\n".join(live_commit_ids).encode()).hexdigest()
        if live_commit_set_digest != item.commit_set_digest:
            raise ValueError("selected lane commit-set digest differs from live Git")
        live_observed_refs = tuple(
            sorted(
                {
                    ref
                    for occurrence_id in item.occurrence_ids
                    for ref in current_by_id[occurrence_id].observed_refs
                }
            )
        )
        if live_observed_refs != item.observed_refs:
            raise ValueError("selected lane logical observed refs differ from live Git")
        expected = classify_commits(manifest, item, live_commits)
        actual = actual_by_logical.get(item.logical_repository_id, [])
        if {_disposition_metadata(entry) for entry in expected} != {
            _disposition_metadata(entry) for entry in actual
        }:
            raise ValueError(
                "selected lane dispositions differ from live qualifying commits"
            )


def _selected_occurrence_state(
    occurrence: RawRepositoryOccurrence,
) -> tuple[Path, str, str, Path | None, str | None, tuple[str, ...], str | None]:
    return (
        occurrence.local_path,
        occurrence.repository_type,
        occurrence.resolution,
        occurrence.git_common_dir,
        occurrence.object_store_id,
        occurrence.observed_refs,
        occurrence.exclusion_reason,
    )


def _validate_selected_evidence(
    manifest: ResearchManifest,
    logical: Sequence[LogicalRepository],
    selected_members: Sequence[str],
    dispositions: Sequence[CommitDisposition],
    selected_dispositions: Sequence[CommitDisposition],
    evidence: Sequence[EvidenceRecord],
) -> None:
    selected_set = set(selected_members)
    if any(not set(item.logical_repository_ids).issubset(selected_set) for item in evidence):
        raise ValueError("evidence record mixes lanes")
    if any(item.state != "complete" for item in evidence):
        raise ValueError("selected evidence state must be complete")
    validate_period_references(manifest, logical, evidence)
    disposition_keys = {
        (item.logical_repository_id, item.commit_id) for item in dispositions
    }
    if any(
        (link.logical_repository_id, link.commit_id) not in disposition_keys
        for item in evidence
        for link in item.links
    ):
        raise ValueError("evidence link references unknown disposition")
    evidence_work_units = {item.work_unit_id for item in evidence}
    if any(
        work_unit_id not in evidence_work_units
        for item in selected_dispositions
        if item.state == "work-unit"
        for work_unit_id in item.work_unit_ids
    ):
        raise ValueError("selected work-unit disposition does not resolve to evidence")


_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|private[_-]?key)\s*[:=]\s*\S+"
)
_URL_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")
_PRIVATE_ABSOLUTE_PATH = re.compile(
    r"(?m)(?:^|[\s(])(?:/(?:Users|home|Volumes|private)/[^\s)]+|[A-Za-z]:\\(?:Users|Documents)\\[^\s)]+)"
)
_PRIVATE_KEY_MARKER = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|OPENSSH PRIVATE KEY",
    re.IGNORECASE,
)


def _validate_evidence_privacy(evidence: Sequence[EvidenceRecord]) -> None:
    for item in evidence:
        texts = [
            item.problem,
            item.technology_use,
            item.personal_contribution,
            item.verification,
            item.outcome,
            item.observed_ref_snapshot,
            *item.conflicts_and_exclusions,
        ]
        texts.extend(link.symbol_or_test for link in item.links)
        if item.decision_episode is not None:
            texts.extend(
                (
                    item.decision_episode.problem,
                    item.decision_episode.constraint_or_alternative,
                    item.decision_episode.selection,
                )
            )
        for text in texts:
            if "```" in text or any(
                line.lstrip().startswith(("diff --git ", "@@ ", "+++ ", "--- "))
                for line in text.splitlines()
            ):
                raise ValueError("privacy: evidence contains a code fence or raw diff")
            if _CREDENTIAL_ASSIGNMENT.search(text):
                raise ValueError("privacy: evidence contains a credential-like assignment")
            if _URL_USERINFO.search(text):
                raise ValueError("privacy: evidence contains URL userinfo")
            if _PRIVATE_ABSOLUTE_PATH.search(text):
                raise ValueError("privacy: evidence contains a private absolute path")
            if _PRIVATE_KEY_MARKER.search(text):
                raise ValueError("privacy: evidence contains a private-key marker")


def _validate_live_inventory(
    manifest: ResearchManifest,
    accepted_occurrences: Sequence[RawRepositoryOccurrence],
    accepted_logical: Sequence[LogicalRepository],
    accepted_dispositions: Sequence[CommitDisposition],
    current_occurrences: Sequence[RawRepositoryOccurrence],
    current_logical: Sequence[LogicalRepository],
    inspector: GitPortfolioInspector,
) -> None:
    accepted_occurrence_state = {
        item.occurrence_id: (
            item.local_path,
            item.repository_type,
            item.git_common_dir,
            item.object_store_id,
            item.resolution,
            item.exclusion_reason,
            item.observed_refs,
        )
        for item in accepted_occurrences
    }
    current_occurrence_state = {
        item.occurrence_id: (
            item.local_path,
            item.repository_type,
            item.git_common_dir,
            item.object_store_id,
            item.resolution,
            item.exclusion_reason,
            item.observed_refs,
        )
        for item in current_occurrences
    }
    if current_occurrence_state != accepted_occurrence_state:
        raise ValueError("current recursive discovery differs from the accepted catalog")

    current_by_occurrences = {
        item.occurrence_ids: item for item in current_logical
    }
    accepted_dispositions_by_logical: dict[str, list[CommitDisposition]] = {}
    for disposition in accepted_dispositions:
        accepted_dispositions_by_logical.setdefault(
            disposition.logical_repository_id,
            [],
        ).append(disposition)
    for accepted in accepted_logical:
        current = current_by_occurrences.get(accepted.occurrence_ids)
        if current is None:
            raise ValueError("current logical repository set differs from the accepted catalog")
        current_identity = (
            current.occurrence_ids,
            current.context_id,
            current.period_ids,
            current.observed_refs,
            current.commit_set_digest,
        )
        accepted_identity = (
            accepted.occurrence_ids,
            accepted.context_id,
            accepted.period_ids,
            accepted.observed_refs,
            accepted.commit_set_digest,
        )
        if current_identity != accepted_identity:
            raise ValueError("current logical repository identity or reachable commit union differs from the accepted catalog")
        commits = inspector.commits(accepted)
        expected = classify_commits(manifest, accepted, commits)
        actual = accepted_dispositions_by_logical.get(accepted.logical_repository_id, [])
        if {_disposition_metadata(item) for item in actual} != {
            _disposition_metadata(item) for item in expected
        }:
            raise ValueError("current attribution or period metadata differs from dispositions")
        qualifying_ids = {item.commit_id for item in expected}
        refresh = classify_refresh(
            accepted.observed_refs,
            current.observed_refs,
            actual,
            commits,
            candidate_commit_ids=qualifying_ids,
        )
        if refresh.decision != "incremental" or not refresh.reason.startswith("0 candidate"):
            raise ValueError("current refresh state is not fully dispositioned")
    if set(current_by_occurrences) != {item.occurrence_ids for item in accepted_logical}:
        raise ValueError("current logical repository set differs from the accepted catalog")


def _disposition_metadata(
    disposition: CommitDisposition,
) -> tuple[str, str, datetime, bool, str, str]:
    return (
        disposition.commit_id,
        disposition.author_identity_id,
        disposition.authored_at,
        disposition.period_matches,
        disposition.merge_marker,
        disposition.evidence_role,
    )


def _discover_approved_occurrences(
    inspector: GitPortfolioInspector,
    approved_roots: Sequence[Path],
) -> list[RawRepositoryOccurrence]:
    resolved_candidates = sorted(
        {path.resolve() for path in approved_roots},
        key=lambda path: (len(path.parts), str(path)),
    )
    resolved_roots = tuple(
        path
        for index, path in enumerate(resolved_candidates)
        if not any(path.is_relative_to(parent) for parent in resolved_candidates[:index])
    )
    occurrences_by_id: dict[str, RawRepositoryOccurrence] = {}
    for root in resolved_roots:
        for occurrence in inspector.discover(root):
            local_path = occurrence.local_path.resolve()
            if not any(local_path.is_relative_to(approved) for approved in resolved_roots):
                raise ValueError("discovery returned an occurrence outside approved roots")
            previous = occurrences_by_id.get(occurrence.occurrence_id)
            if previous is not None and previous != occurrence:
                raise ValueError("duplicate occurrence ID has inconsistent observations")
            occurrences_by_id[occurrence.occurrence_id] = occurrence
    return sorted(occurrences_by_id.values(), key=lambda item: str(item.local_path))


_LANE_IDS = frozenset(f"lane-{index}" for index in range(1, 5))
_LANE_STATUSES = frozenset({"cataloged", "complete"})


def render_lane(
    lane_id: str,
    logical_repository_ids: Sequence[str],
    status: str,
) -> str:
    members = tuple(logical_repository_ids)
    if lane_id not in _LANE_IDS:
        raise ValueError("lane_id must be lane-1 through lane-4")
    if status not in _LANE_STATUSES:
        raise ValueError("unknown lane status")
    if (
        any(not isinstance(item, str) or not item for item in members)
        or len(members) != len(set(members))
    ):
        raise ValueError("lane logical_repository_ids must be unique non-blank strings")
    rendered_members = (
        "logical_repository_ids:\n"
        + "".join(f"- {logical_id}\n" for logical_id in members)
        if members
        else "logical_repository_ids: []\n"
    )
    return (
        "---\n"
        "schema: portfolio-lane/v1\n"
        f"lane_id: {lane_id}\n"
        f"{rendered_members}"
        f"status: {status}\n"
        "---\n"
        "# Portfolio research lane\n"
    )


def _load_lane(
    path: Path,
    journals: Sequence[PublicationResult],
) -> tuple[str, tuple[str, ...], str]:
    ensure_readable(path, journals)
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name} must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0 or text[end + 5 :] != "# Portfolio research lane\n":
        raise ValueError(f"{path.name} has an invalid lane body")
    try:
        raw = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} has invalid lane frontmatter") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "lane_id",
        "logical_repository_ids",
        "status",
    }:
        raise ValueError(f"{path.name} lane fields must match exactly")
    lane_id = raw["lane_id"]
    members = raw["logical_repository_ids"]
    if (
        raw["schema"] != "portfolio-lane/v1"
        or not isinstance(lane_id, str)
        or lane_id != path.stem
        or lane_id not in _LANE_IDS
        or not isinstance(members, list)
        or any(not isinstance(item, str) or not item for item in members)
        or len(members) != len(set(members))
        or not isinstance(raw["status"], str)
        or raw["status"] not in _LANE_STATUSES
    ):
        raise ValueError(f"{path.name} has an invalid lane contract")
    status = raw["status"]
    if text != render_lane(lane_id, members, status):
        raise ValueError(f"{path.name} has a non-canonical lane document")
    return lane_id, tuple(members), status


STALE_DISPOSITION_COUNT = 25
T1_EVIDENCE_FILENAMES = frozenset(
    {
        "T1-success.md",
        "T1-forced-failure.md",
        "T1-rerun.md",
        "T1-compensation.md",
        "T1-headless.md",
        "T1-cancellation.md",
    }
)
_PRIVATE_EVIDENCE_PATTERN = re.compile(
    r"/Users/|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+|(?<![0-9a-f])[0-9a-f]{40,64}(?![0-9a-f])"
)


def render_stale_disposition_inventory(inventory: Mapping[Path, str]) -> str:
    lines = [
        "# U2 stale disposition inventory",
        "",
        f"count: {len(inventory)}",
        "",
        "| Relative path | SHA-256 |",
        "| --- | --- |",
    ]
    for path, digest in sorted(inventory.items(), key=lambda item: item[0].as_posix()):
        _validate_stale_relative_path(path)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("stale inventory digest must be lowercase SHA-256")
        lines.append(f"| {path.as_posix()} | {digest} |")
    return "\n".join(lines) + "\n"


def load_stale_disposition_inventory(path: Path) -> dict[Path, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 6 or lines[:2] != ["# U2 stale disposition inventory", ""]:
        raise ValueError("stale inventory heading is invalid")
    try:
        count = int(lines[2].removeprefix("count: "))
    except ValueError as exc:
        raise ValueError("stale inventory count is invalid") from exc
    if lines[3:6] != ["", "| Relative path | SHA-256 |", "| --- | --- |"]:
        raise ValueError("stale inventory table header is invalid")
    inventory: dict[Path, str] = {}
    for line in lines[6:]:
        if not line.startswith("| ") or not line.endswith(" |"):
            raise ValueError("stale inventory row is invalid")
        cells = line[2:-2].split(" | ")
        if len(cells) != 2:
            raise ValueError("stale inventory row is invalid")
        relative_path = Path(cells[0])
        digest = cells[1]
        _validate_stale_relative_path(relative_path)
        if relative_path in inventory:
            raise ValueError("duplicate stale inventory path")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("stale inventory digest must be lowercase SHA-256")
        inventory[relative_path] = digest
    if count != len(inventory):
        raise ValueError("stale inventory count does not match rows")
    return inventory


def prepare_stale_disposition_cleanup(
    root: Path,
    corrected_logical_ids: Set[str],
    superseded_journal: PublicationResult,
    approved_inventory: Mapping[Path, str],
) -> tuple[PendingWrite, ...]:
    root = root.resolve()
    dispositions_root = root / "dispositions"
    if superseded_journal.state != "committed":
        raise ValueError("superseded journal must be committed")
    inventory_paths = set(approved_inventory)
    if len(inventory_paths) != STALE_DISPOSITION_COUNT:
        raise ValueError("exact stale allowlist must contain 25 entries")
    for target in inventory_paths:
        _validate_stale_relative_path(target)
    absent_from_catalog = {
        path.relative_to(root)
        for path in dispositions_root.glob("*.md")
        if path.stem not in corrected_logical_ids
    }
    journal_owned = {
        write.target
        for write in superseded_journal.writes
        if len(write.target.parts) == 2 and write.target.parts[0] == "dispositions"
    }
    if absent_from_catalog & journal_owned & inventory_paths != inventory_paths:
        raise ValueError("exact stale allowlist must match catalog absence and journal ownership")

    replacement = render_dispositions(())
    output_digest = sha256(replacement.encode()).hexdigest()
    writes: list[PendingWrite] = []
    for target in sorted(inventory_paths, key=Path.as_posix):
        path = root / target
        if path.is_symlink() or not path.is_file():
            raise ValueError("stale target must be a regular non-symlink file")
        if not path.resolve().is_relative_to(dispositions_root.resolve()):
            raise ValueError("stale target must remain inside dispositions")
        original = path.read_text(encoding="utf-8")
        digest = sha256(original.encode()).hexdigest()
        if digest != approved_inventory[target]:
            raise ValueError("stale target digest does not match approved inventory")
        writes.append(
            PendingWrite(target, original, replacement, digest, output_digest)
        )
    return tuple(writes)


def snapshot_stale_dispositions(
    root: Path,
    writes: Sequence[PendingWrite],
    snapshot_root: Path,
) -> None:
    root = root.resolve()
    expected_root = root / "runtime-evidence" / "U2"
    if snapshot_root.is_symlink() or not snapshot_root.resolve().is_relative_to(expected_root.resolve()):
        raise ValueError("snapshot root must remain inside private U2 runtime evidence")
    inventory: dict[Path, str] = {}
    for write in writes:
        source = root / write.target
        if write.original_text is None or write.pre_state_digest is None:
            raise ValueError("stale snapshot requires an existing pre-state")
        if source.is_symlink() or not source.is_file():
            raise ValueError("stale snapshot source must be a regular non-symlink file")
        if sha256(source.read_bytes()).hexdigest() != write.pre_state_digest:
            raise ValueError("stale snapshot source digest changed")
        snapshot = snapshot_root / write.target.name
        atomic_write(snapshot, write.original_text)
        if sha256(snapshot.read_bytes()).hexdigest() != write.pre_state_digest:
            raise ValueError("stale snapshot digest mismatch")
        inventory[write.target] = write.pre_state_digest
    atomic_write(snapshot_root / "snapshot-inventory.md", render_stale_disposition_inventory(inventory))


def cleanup_stale_dispositions(
    root: Path,
    corrected_logical_ids: Set[str],
    superseded_journal: PublicationResult,
    approved_inventory: Mapping[Path, str],
    snapshot_root: Path,
    journal: Path,
) -> PublicationResult:
    writes = prepare_stale_disposition_cleanup(
        root,
        corrected_logical_ids,
        superseded_journal,
        approved_inventory,
    )
    snapshot_stale_dispositions(root, writes, snapshot_root)
    return publish_transaction(writes, journal)


def prepare_stale_cleanup_headless(
    root: Path,
    corrected_logical_ids: Set[str],
    superseded_journal: PublicationResult,
    approved_inventory: Mapping[Path, str],
    inventory_path: Path,
    proposal_path: Path,
) -> PublicationResult:
    root = root.resolve()
    runtime_root = root / "runtime-evidence" / "U2"
    for path in (inventory_path, proposal_path):
        if path.is_symlink() or not path.resolve().is_relative_to(runtime_root.resolve()):
            raise ValueError("headless outputs must remain inside private U2 runtime evidence")
    writes = prepare_stale_disposition_cleanup(
        root,
        corrected_logical_ids,
        superseded_journal,
        approved_inventory,
    )
    atomic_write(inventory_path, render_stale_disposition_inventory(approved_inventory))
    prepared = PublicationResult(
        proposal_path.stem,
        "prepared",
        writes,
        proposal_path.relative_to(root),
    )
    atomic_write(proposal_path, render_publication_result(prepared))
    return prepared


def write_sanitized_t1_evidence(code_root: Path, records: Mapping[str, str]) -> Path:
    if set(records) != T1_EVIDENCE_FILENAMES:
        raise ValueError("T1 evidence filenames must match the six approved outcomes")
    evidence_root = code_root / ".release-loop" / "evidence" / "portfolio-U2"
    for filename, text in records.items():
        if _PRIVATE_EVIDENCE_PATTERN.search(text):
            raise ValueError("sanitized evidence contains private detail")
        atomic_write(evidence_root / filename, text)
    receipt = "# U2 T1 receipt\n\n" + "".join(
        f"- {filename.removeprefix('T1-').removesuffix('.md')}: pass\n"
        for filename in sorted(records)
    )
    atomic_write(evidence_root / "receipt.md", receipt)
    return evidence_root


def _validate_stale_relative_path(path: Path) -> None:
    if (
        path.is_absolute()
        or path.parts != ("dispositions", path.name)
        or path.name in {"", ".", ".."}
        or path.suffix != ".md"
    ):
        raise ValueError("stale inventory path must be one disposition Markdown file")


def _refs_by_name(refs: Sequence[str]) -> dict[str, str] | None:
    values: dict[str, str] = {}
    for value in refs:
        if "=" not in value:
            return None
        name, object_id = value.rsplit("=", 1)
        if not name or not object_id:
            return None
        values[name] = object_id
    return values


def _is_ancestor(
    ancestor_id: str,
    descendant_id: str,
    commits_by_id: Mapping[str, CommitObservation],
) -> bool:
    pending = [descendant_id]
    visited: set[str] = set()
    while pending:
        commit_id = pending.pop()
        if commit_id == ancestor_id:
            return True
        if commit_id in visited:
            continue
        visited.add(commit_id)
        commit = commits_by_id.get(commit_id)
        if commit is not None:
            pending.extend(commit.parent_ids)
    return False
