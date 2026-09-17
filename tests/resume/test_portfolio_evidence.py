from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from careerkit.resume.domain import portfolio_evidence
from careerkit.resume.domain.portfolio_evidence import (
    AuthorIdentity,
    CalibrationResult,
    COMMIT_EXCLUSION_REASONS,
    CommitDisposition,
    ComprehensiveEvalResult,
    ContextMapping,
    DecisionEpisode,
    EmploymentPeriod,
    EvalCase,
    EvalClaim,
    EvalActivationReceipt,
    EvalComparison,
    EvalGate,
    EvalInput,
    EvidenceLink,
    EvidenceRecord,
    GoldClaim,
    Judgment,
    LogicalRepository,
    NarrativeProposal,
    PendingWrite,
    PortfolioEvalOutput,
    PublicationResult,
    RawRepositoryOccurrence,
    RefreshDecision,
    ResearchManifest,
    ValidationIssue,
)


UTC = timezone.utc


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def test_personal_context_addendum_changes_period_and_root_contracts() -> None:
    assert [field.name for field in fields(EmploymentPeriod)] == [
        "period_id",
        "employment_label",
        "starts_at",
        "ends_at",
    ]
    assert [field.name for field in fields(ContextMapping)] == [
        "context_id",
        "context_type",
        "discovery_roots",
        "narrative_root",
        "period_ids",
    ]
    assert "period_ids" in {field.name for field in fields(LogicalRepository)}
    assert "period_ids" in {field.name for field in fields(EvidenceRecord)}


def test_domain_records_are_immutable_and_normalize_containers() -> None:
    author = AuthorIdentity("author-1", "Example Author", "author@example.test")
    period = EmploymentPeriod(
        "period-1",
        "company-1",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 12, 31, 23, 59, 59, tzinfo=UTC),
    )
    context = ContextMapping("company-1", "company", (Path("/approved/company"),), Path("companies/c1"), ("period-1",))
    manifest = ResearchManifest("portfolio-manifest/v1", (context,), (author,), (period,))

    assert manifest.contexts == (context,)
    with pytest.raises(FrozenInstanceError):
        author.name = "Changed"  # type: ignore[misc]


def test_employment_period_uses_inclusive_timezone_aware_boundaries() -> None:
    start = datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=9)))
    end = datetime(2020, 12, 31, 23, 59, 59, tzinfo=timezone(timedelta(hours=9)))
    period = EmploymentPeriod("period-1", "company-1", start, end)

    assert period.contains(start)
    assert period.contains(end)
    assert not period.contains(start - timedelta(microseconds=1))
    assert not period.contains(end + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        EmploymentPeriod("period-1", "company-1", start.replace(tzinfo=None), end)


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: RawRepositoryOccurrence("o1", Path("repo"), "standard", Path("g"), "obj", (), None, None, "other"), "resolution"),
        (lambda: CommitDisposition("r1", "a" * 40, "author-1", datetime.now(UTC), True, "yes", "no", "no", "no", "other", (), None, "contribution"), "state"),
        (lambda: EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "maybe"), "role"),
        (lambda: EvidenceRecord("e1", "company-1", "project-1", "work-1", ("period-1",), ("r1",), ("a" * 40,), "yes", "yes", "p", "t", "c", "v", "o", (), (EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution"),), "refs", "d" * 64, "ready", "none"), "state"),
        (lambda: NarrativeProposal("p1", Path("companies/c1/p.md"), _sha("before"), Path("snapshots/p1/p.md"), _sha("before"), "before", "after", ("e1",), "pending", None, None), "state"),
        (lambda: RefreshDecision("other", "reason"), "decision"),
        (lambda: EvalInput("other", "v1", "abc"), "kind"),
        (lambda: Judgment("other", (), "note"), "verdict"),
    ],
)
def test_closed_values_reject_unknown_members(factory: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()  # type: ignore[operator]


def test_relative_reference_fields_reject_absolute_and_traversing_paths() -> None:
    valid = EvidenceLink("repo-1", "a" * 40, Path("src/module.py"), "symbol", "contribution")
    assert valid.relative_path == Path("src/module.py")
    for invalid in (Path("../src/module.py"), Path("/src/module.py")):
        with pytest.raises(ValueError, match="relative_path"):
            EvidenceLink("repo-1", "a" * 40, invalid, "symbol", "contribution")


def test_cross_field_invariants_and_duplicate_ids_fail_closed() -> None:
    context = ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1",))
    author = AuthorIdentity("a1", "Author", "a@example.test")
    period = EmploymentPeriod("p1", "c1", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="duplicate context_id"):
        ResearchManifest("portfolio-manifest/v1", (context, context), (author,), (period,))
    with pytest.raises(ValueError, match="work_unit_ids"):
        CommitDisposition("r1", "a" * 40, "a1", datetime.now(UTC), True, "no", "no", "no", "no", "work-unit", (), None, "contribution")
    with pytest.raises(ValueError, match="exclusion_reason"):
        CommitDisposition("r1", "a" * 40, "a1", datetime.now(UTC), True, "no", "no", "no", "no", "excluded", (), None, "context")


def test_personal_context_accepts_three_roots_and_several_periods() -> None:
    periods = (
        EmploymentPeriod("p1", "Employment A", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment B", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    context = ContextMapping(
        "personal",
        "personal-open-source",
        (Path("/approved/one"), Path("/approved/two"), Path("/approved/three")),
        Path("portfolios/personal"),
        ("p1", "p2"),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (context,),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        periods,
    )

    assert manifest.contexts[0].discovery_roots == context.discovery_roots
    assert manifest.contexts[0].period_ids == ("p1", "p2")
    assert periods[0].employment_label == "Employment A"


def test_employment_label_does_not_require_a_matching_discovery_context() -> None:
    period = EmploymentPeriod(
        "p1",
        "Unmapped Employment",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2021, 1, 1, tzinfo=UTC),
    )
    context = ContextMapping("personal", "personal-open-source", (Path("/approved"),), Path("portfolios"), ("p1",))

    ResearchManifest(
        "portfolio-manifest/v1",
        (context,),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        (period,),
    )


def test_context_roots_and_period_ids_must_be_non_empty_and_unique() -> None:
    with pytest.raises(ValueError, match="discovery_roots must not be empty"):
        ContextMapping("c1", "company", (), Path("companies/c1"), ("p1",))
    with pytest.raises(ValueError, match="period_ids must not be empty"):
        ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ())
    with pytest.raises(ValueError, match="duplicate discovery_root"):
        ContextMapping("c1", "company", (Path("/approved"), Path("/approved")), Path("companies/c1"), ("p1",))
    with pytest.raises(ValueError, match="duplicate period_id"):
        ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1", "p1"))


def test_manifest_rejects_duplicate_roots_across_contexts_and_unknown_context_periods() -> None:
    author = AuthorIdentity("a1", "Author", "a@example.test")
    periods = (
        EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    first = ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1",))
    duplicate = ContextMapping("c2", "company", (Path("/approved"),), Path("companies/c2"), ("p1",))
    unknown = ContextMapping("c2", "company", (Path("/other"),), Path("companies/c2"), ("missing",))
    with pytest.raises(ValueError, match="duplicate discovery_root"):
        ResearchManifest("portfolio-manifest/v1", (first, duplicate), (author,), periods)
    with pytest.raises(ValueError, match="unknown period_id"):
        ResearchManifest("portfolio-manifest/v1", (first, unknown), (author,), periods)


def test_repository_and_evidence_period_ids_must_be_non_empty_and_unique() -> None:
    logical = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "complete", "none")
    with pytest.raises(ValueError, match="period_ids must not be empty"):
        replace(logical, period_ids=())
    with pytest.raises(ValueError, match="duplicate period_id"):
        replace(logical, period_ids=("p1", "p1"))
    with pytest.raises(ValueError, match="period_ids must not be empty"):
        replace(evidence, period_ids=())
    with pytest.raises(ValueError, match="duplicate period_id"):
        replace(evidence, period_ids=("p1", "p1"))


def test_period_reference_validation_rejects_unknown_and_repository_mismatches() -> None:
    periods = (
        EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1",)),),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        periods,
    )
    logical = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "complete", "none")

    portfolio_evidence.validate_period_references(manifest, (logical,), (evidence,))
    with pytest.raises(ValueError, match="logical repository.*unknown period_id"):
        portfolio_evidence.validate_period_references(manifest, (replace(logical, period_ids=("missing",)),), ())
    with pytest.raises(ValueError, match="evidence.*unknown period_id"):
        portfolio_evidence.validate_period_references(manifest, (logical,), (replace(evidence, period_ids=("missing",)),))
    with pytest.raises(ValueError, match="logical repository period_ids"):
        portfolio_evidence.validate_period_references(
            manifest,
            (replace(logical, period_ids=("p1",)),),
            (replace(evidence, period_ids=("p2",), logical_repository_ids=("r1",)),),
        )


def test_period_reference_validation_rejects_period_outside_logical_context() -> None:
    periods = (
        EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1",)),),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        periods,
    )
    logical = LogicalRepository("r1", ("o1",), "c1", ("p2",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)

    with pytest.raises(ValueError, match="logical repository.*context period_ids"):
        portfolio_evidence.validate_period_references(manifest, (logical,), ())
    with pytest.raises(ValueError, match="unknown context_id"):
        portfolio_evidence.validate_period_references(
            manifest,
            (replace(logical, context_id="missing", period_ids=("p1",)),),
            (),
        )


def test_period_reference_validation_accepts_union_across_repositories() -> None:
    periods = (
        EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1", "p2")),),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        periods,
    )
    first = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    second = replace(first, logical_repository_id="r2", occurrence_ids=("o2",), period_ids=("p2",))
    links = (
        EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution"),
        EvidenceLink("r2", "b" * 40, Path("src/b.py"), "symbol", "contribution"),
    )
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1", "p2"), ("r1", "r2"), ("a" * 40, "b" * 40), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), links, "refs", "d" * 64, "complete", "none")

    portfolio_evidence.validate_period_references(manifest, (first, second), (evidence,))


def test_period_reference_validation_rejects_unknown_evidence_context() -> None:
    period = EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC))
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("c1", "company", (Path("/approved"),), Path("companies/c1"), ("p1",)),),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        (period,),
    )
    logical = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    evidence = EvidenceRecord("e1", "missing", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "complete", "none")

    with pytest.raises(ValueError, match="evidence references unknown context_id"):
        portfolio_evidence.validate_period_references(manifest, (logical,), (evidence,))


def test_period_reference_validation_rejects_cross_context_repository_union() -> None:
    periods = (
        EmploymentPeriod("p1", "Employment", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (
            ContextMapping("c1", "company", (Path("/approved/one"),), Path("companies/c1"), ("p1",)),
            ContextMapping("c2", "company", (Path("/approved/two"),), Path("companies/c2"), ("p2",)),
        ),
        (AuthorIdentity("a1", "Author", "a@example.test"),),
        periods,
    )
    first = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    second = replace(first, logical_repository_id="r2", occurrence_ids=("o2",), context_id="c2", period_ids=("p2",))
    links = (
        EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution"),
        EvidenceLink("r2", "b" * 40, Path("src/b.py"), "symbol", "contribution"),
    )
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1", "p2"), ("r1", "r2"), ("a" * 40, "b" * 40), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), links, "refs", "d" * 64, "complete", "none")

    with pytest.raises(ValueError, match="evidence and logical repository context_id values must match"):
        portfolio_evidence.validate_period_references(manifest, (first, second), (evidence,))


def test_continuous_and_boundary_separated_work_units_are_representable() -> None:
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    shared = EvidenceRecord("e-shared", "c1", "project-1", "work-shared", ("p1", "p2"), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "complete", "none")
    before = replace(shared, evidence_id="e-before", work_unit_id="work-before", period_ids=("p1",))
    after = replace(shared, evidence_id="e-after", work_unit_id="work-after", period_ids=("p2",))

    assert shared.period_ids == ("p1", "p2")
    assert before.work_unit_id != after.work_unit_id
    assert before.period_ids != after.period_ids


def test_all_declared_records_have_minimal_valid_construction() -> None:
    occurrence = RawRepositoryOccurrence("o1", Path("repo"), "standard", Path("git"), "objects", ("refs/heads/main=" + "a" * 40,), "r1", None, "resolved")
    logical = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 2, "cataloged", "pending", "reason", None, ("refs/heads/main=" + "a" * 40,), "d" * 64)
    pending = PendingWrite(Path("companies/c1/p.md"), "before", "after", _sha("before"), _sha("after"))
    publication = PublicationResult("tx-1", "committed", (replace(pending, state="replaced"),), Path("transactions/tx-1.md"))
    case = EvalCase("case-1", "supported", "c1", "prompt", ("claim-1",), (), ("e1",))
    gate = EvalGate("smoke", (EvalInput("dataset", "v1", "d" * 64),), 5, 0)
    gold = GoldClaim("claim-1", "supported", "supported fact", ("e1",), "c1")
    calibration = CalibrationResult("run-1", 24, 0.95, 1.0, 0.8, True)
    issue = ValidationIssue("error", "invalid_reference", "message", Path("evidence/e1.md"))

    assert occurrence.resolution == "resolved"
    assert logical.qualifying_commit_count == 2
    assert pending.target.is_absolute() is False
    assert publication.state == "committed"
    assert case.case_id == "case-1"
    assert gate.minimum_runs == 5
    assert gold.claim_id == "claim-1"
    assert calibration.passed
    assert issue.code == "invalid_reference"


def test_zero_commit_repository_omits_commit_boundaries() -> None:
    empty = LogicalRepository("r1", ("o1",), "c1", ("p1",), "no", None, None, 0, "excluded", "excluded", "no commits", None, (), "d" * 64)
    assert empty.first_commit_id is None
    with pytest.raises(ValueError, match="commit boundaries"):
        LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", None, "b" * 40, 1, "cataloged", "selected", "reason", None, (), "d" * 64)


def test_unresolved_occurrence_does_not_fabricate_git_observations() -> None:
    unresolved = RawRepositoryOccurrence("o1", Path("missing"), "standard", None, None, (), None, "inaccessible", "unresolved")
    assert unresolved.git_common_dir is None
    with pytest.raises(ValueError, match="Git observations"):
        RawRepositoryOccurrence("o1", Path("repo"), "standard", None, None, (), "r1", None, "resolved")


def test_publication_records_bind_pre_and_output_digests_and_commit_state() -> None:
    write = PendingWrite(Path("catalog.md"), "before", "after", _sha("before"), _sha("after"))
    committed = PublicationResult("tx-1", "committed", (replace(write, state="replaced"),), Path("transactions/tx-1.md"))
    recovery = PublicationResult("tx-2", "manual-recovery", (replace(write, state="manual-recovery"),), Path("transactions/tx-2.md"))
    assert not committed.blocks(Path("catalog.md"))
    assert recovery.blocks(Path("catalog.md"))
    created = PendingWrite(Path("new.md"), None, "new", None, _sha("new"))
    assert created.pre_state_digest is None
    with pytest.raises(ValueError, match="output_digest"):
        PendingWrite(Path("catalog.md"), "before", "after", _sha("before"), "bad")
    with pytest.raises(ValueError, match="match replacement_text"):
        PendingWrite(Path("catalog.md"), "before", "after", _sha("before"), "b" * 64)


def test_proposal_recovery_fields_follow_state() -> None:
    common = {
        "proposal_id": "n1",
        "source_path": Path("companies/c1/portfolios/p.md"),
        "source_digest": _sha("old"),
        "snapshot_path": Path("snapshots/n1/p.md"),
        "snapshot_digest": _sha("old"),
        "original_text": "old",
        "proposed_text": "new [e1]",
        "evidence_ids": ("e1",),
        "approved_by": "reviewer",
        "approved_at": datetime(2024, 1, 1, tzinfo=UTC),
    }
    applied = NarrativeProposal(**common, state="applied", applied_output_digest=_sha("new [e1]"))
    assert applied.applied_output_digest == _sha("new [e1]")
    with pytest.raises(ValueError, match="applied_output_digest"):
        NarrativeProposal(**common, state="approved", applied_output_digest=_sha("new [e1]"))


def test_structured_decision_episode_and_complete_content_guards() -> None:
    decision = DecisionEpisode("problem", "constraint", "selection", ("e1",))
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "parse", "contribution")
    record = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "pytest", "outcome", (), (link,), "refs", "d" * 64, "complete", "architecture", decision)
    assert record.decision_episode == decision
    with pytest.raises(ValueError, match="code fences"):
        replace(record, verification="```text\noutput\n```")


def test_eval_context_forbidden_claims_and_gold_labels() -> None:
    case = EvalCase("case-1", "supported", "c1", "prompt", ("claim-1",), ("forbidden",), ("e1",))
    assert case.forbidden_claims == ("forbidden",)
    assert GoldClaim("claim-1", "supported", "fact", ("e1",), "c1").label == "supported"
    assert GoldClaim("claim-2", "unsupported", "distractor", (), "c1").evidence_ids == ()
    with pytest.raises(ValueError, match="unsupported"):
        GoldClaim("claim-2", "unsupported", "distractor", ("e1",), "c1")


def test_portfolio_eval_output_requires_fact_and_context_metadata() -> None:
    claim = EvalClaim("supported fact", ("e1",), ("claim-1",), "c1")
    output = PortfolioEvalOutput("case-1", (claim,), ())

    assert output.claims == (claim,)
    with pytest.raises(ValueError, match="fact_ids"):
        EvalClaim("unsupported text", ("e1",), (), "c1")
    with pytest.raises(ValueError, match="omitted_claims"):
        PortfolioEvalOutput("case-1", (), ("",))


def test_calibration_and_comprehensive_results_have_distinct_metrics() -> None:
    calibration = CalibrationResult("cal-1", 24, 0.95, 1.0, 0.8, True)
    case_ids = tuple(f"case-{index}" for index in range(12))
    comprehensive = ComprehensiveEvalResult(
        "run-1",
        "pass",
        60,
        0,
        0.0488,
        tuple((case_id, 5) for case_id in case_ids),
        100,
        100,
        100,
        1.0,
        0.0,
        case_ids,
    )
    assert calibration.cohens_kappa == 0.8
    assert comprehensive.output_count == 60
    with pytest.raises(ValueError, match="12 expected case IDs"):
        ComprehensiveEvalResult("run-invalid", "pass", 60, 0, 0.0488, (("case-1", 60),), 100, 100, 100, 1.0, 0.0)
    with pytest.raises(ValueError, match="thresholds"):
        CalibrationResult("cal-2", 24, 0.94, 1.0, 0.8, True)
    with pytest.raises(ValueError, match="pass verdict"):
        ComprehensiveEvalResult(
            "run-2",
            "pass",
            60,
            1,
            0.06,
            tuple((case_id, 5) for case_id in case_ids),
            100,
            100,
            100,
            1.0,
            0.0,
            case_ids,
        )


def test_commit_exclusion_reason_uses_allowlist() -> None:
    with pytest.raises(ValueError, match="exclusion_reason"):
        CommitDisposition("r1", "a" * 40, "a1", datetime.now(UTC), True, "no", "no", "no", "no", "excluded", (), "invented", "context")


@pytest.mark.parametrize("path", [Path("C:/private/file.py"), Path("C:\\private\\file.py"), Path("\\\\server\\share\\file.py")])
def test_relative_paths_reject_windows_absolute_forms(path: Path) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        EvidenceLink("r1", "a" * 40, path, "symbol", "contribution")


def test_every_closed_value_has_an_accepted_representative() -> None:
    for context_type in ("company", "personal-open-source"):
        ContextMapping("c1", context_type, (Path("/approved"),), Path("portfolios"), ("p1",))
    for repository_type in ("standard", "worktree", "submodule", "bare"):
        RawRepositoryOccurrence("o1", Path("repo"), repository_type, Path("git"), "objects", (), "r1", None, "resolved")
    RawRepositoryOccurrence("o1", Path("repo"), "standard", None, None, (), None, "inaccessible", "unresolved")

    logical = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    for status, decision in (("unscanned", "pending"), ("cataloged", "pending"), ("selected", "selected"), ("excluded", "excluded"), ("evidenced", "selected"), ("reconciled", "selected"), ("complete", "selected")):
        replace(logical, research_status=status, deep_research_decision=decision)

    base_disposition = CommitDisposition("r1", "a" * 40, "a1", datetime.now(UTC), True, "no", "unknown", "yes", "no", "unassigned", (), None, "contribution")
    replace(base_disposition, state="work-unit", work_unit_ids=("w1",))
    for reason in COMMIT_EXCLUSION_REASONS:
        replace(base_disposition, state="excluded", exclusion_reason=reason, evidence_role="context")
    for role in ("contribution", "context"):
        replace(base_disposition, evidence_role=role)

    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "unknown", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "draft", "none")
    for state in ("draft", "verified", "conflict", "complete"):
        replace(evidence, state=state, period_check="yes" if state == "complete" else "unknown")
    decision_episode = DecisionEpisode("problem", "constraint", "selection", ("e1",))
    for decision_type in ("architecture", "technology", "process", "quality", "operations"):
        replace(evidence, decision_type=decision_type, decision_episode=decision_episode)

    proposal_common = ("n1", Path("source.md"), _sha("old"), Path("snapshots/n1/source.md"), _sha("old"), "old", "new", ("e1",))
    NarrativeProposal(*proposal_common, "draft", None, None)
    NarrativeProposal(*proposal_common, "approved", "reviewer", datetime.now(UTC))
    NarrativeProposal(*proposal_common, "applied", "reviewer", datetime.now(UTC), _sha("new"))
    NarrativeProposal(*proposal_common, "rejected", None, None)

    for decision in ("incremental", "full-reconciliation", "blocked"):
        RefreshDecision(decision, "reason")
    for kind in ("narrative", "index", "evidence", "instruction", "model", "dataset", "grader", "rubric"):
        EvalInput(kind, "v1", "d" * 64)
    for verdict in ("pass", "fail", "insufficient_data", "unavailable"):
        Judgment(verdict, (), "note")
    for category in ("supported", "unsupported-distractor", "missing-rationale", "context-separation"):
        EvalCase("case-1", category, "c1", "prompt", (), (), ())
    eval_input = EvalInput("dataset", "v1", "d" * 64)
    for gate in ("smoke", "comprehensive"):
        EvalGate(gate, (eval_input,), 1, 0)
    GoldClaim("claim-1", "supported", "fact", ("e1",), "c1")
    GoldClaim("claim-2", "unsupported", "distractor", (), "c1")

    write = PendingWrite(Path("record.md"), "old", "new", _sha("old"), _sha("new"))
    journal_states = (("prepared", "pending"), ("replacing", "replaced"), ("compensating", "replaced"), ("committed", "replaced"), ("compensated", "compensated"), ("manual-recovery", "manual-recovery"))
    for state, write_state in journal_states:
        PublicationResult("tx-1", state, (replace(write, state=write_state),), Path("transactions/tx-1.md"))


def test_publication_journal_represents_every_replacement_boundary() -> None:
    writes = tuple(
        PendingWrite(Path(f"record-{index}.md"), "old", "new", _sha("old"), _sha("new"), "pending")
        for index in range(3)
    )
    PublicationResult("tx-0", "prepared", writes, Path("transactions/tx-0.md"))
    for completed_count in range(1, len(writes)):
        boundary = tuple(
            replace(write, state="replaced" if index < completed_count else "pending")
            for index, write in enumerate(writes)
        )
        result = PublicationResult("tx-boundary", "replacing", boundary, Path("transactions/tx-boundary.md"))
        assert result.completed_targets == tuple(write.target for write in boundary[:completed_count])
    committed = tuple(replace(write, state="replaced") for write in writes)
    PublicationResult("tx-final", "committed", committed, Path("transactions/tx-final.md"))
    with pytest.raises(ValueError, match="committed"):
        PublicationResult("tx-bad", "committed", writes, Path("transactions/tx-bad.md"))
    compensating = (
        replace(writes[0], state="replaced"),
        replace(writes[1], state="compensated"),
        writes[2],
    )
    PublicationResult("tx-compensate", "compensating", compensating, Path("transactions/tx-compensate.md"))
    with pytest.raises(ValueError, match="per-write states"):
        PublicationResult("tx-forward", "compensating", tuple(reversed(compensating)), Path("transactions/tx-forward.md"))


def test_comprehensive_receipt_enforces_all_r34_to_r47_thresholds() -> None:
    case_counts = tuple((f"case-{index}", 5) for index in range(12))
    passing = ComprehensiveEvalResult(
        "run-1", "pass", 60, 0, 0.0488, case_counts,
        1_800_000, 60_000, 800, 300.0, 0.20,
        tuple(f"case-{index}" for index in range(12)),
    )
    assert passing.passed
    failure_changes = (
        {"error_count": 1},
        {"input_tokens": 1_800_001},
        {"output_tokens": 60_001},
        {"max_output_tokens": 801},
        {"p95_latency_seconds": 300.1},
        {"comparable_baseline_regression": 0.201},
        {"per_case_run_counts": (("case-0", 4), ("case-1", 6), *case_counts[2:])},
    )
    for changes in failure_changes:
        with pytest.raises(ValueError, match="pass verdict"):
            replace(passing, **changes)


def test_comprehensive_receipt_failure_verdicts_are_representable() -> None:
    case_ids = tuple(f"case-{index}" for index in range(12))
    case_counts = tuple((case_id, 5) for case_id in case_ids)
    ComprehensiveEvalResult("run-fail", "fail", 60, 1, 0.06, case_counts, 10, 10, 10, 1.0, 0.0, case_ids)
    incomplete_counts = tuple((case_id, 0) for case_id in case_ids[:-1]) + ((case_ids[-1], 10),)
    ComprehensiveEvalResult("run-more", "insufficient_data", 10, 0, 0.25, incomplete_counts, 10, 10, 10, 1.0, 0.0, case_ids)
    ComprehensiveEvalResult("run-down", "unavailable", 0, 0, None, (), None, None, None, None, None)


def test_eval_activation_receipt_rejects_unknown_verdict() -> None:
    with pytest.raises(ValueError, match="verdict"):
        EvalActivationReceipt(
            "run-1",
            "comprehensive",
            "unknown",
            (EvalInput("grader", "v2", "a" * 64),),
            datetime.now(UTC),
            60,
            0,
            "b" * 64,
        )


def test_eval_comparison_is_a_typed_resource_signal() -> None:
    comparison = EvalComparison(True, 0.0, 0.0, 0.0)

    assert comparison.correctness_equal
    with pytest.raises(ValueError, match="regression"):
        EvalComparison(True, -1.1, 0.0, 0.0)


@pytest.mark.parametrize(
    "observed_ref",
    [
        "main=" + "a" * 40,
        "refs/heads/main=not-hex",
        "refs/heads/../main=" + "a" * 40,
        "head=" + "a" * 40,
        "HEAD=" + "A" * 40,
        "HEAD=" + "a" * 39,
        "HEAD=" + "a" * 65,
    ],
)
def test_observed_refs_reject_malformed_git_output(observed_ref: str) -> None:
    with pytest.raises(ValueError, match="observed_ref"):
        RawRepositoryOccurrence("o1", Path("repo"), "standard", Path("git"), "objects", (observed_ref,), "r1", None, "resolved")


def test_observed_refs_accept_exact_detached_head_pseudo_ref() -> None:
    detached_head = "HEAD=" + "a" * 40
    occurrence = RawRepositoryOccurrence(
        "o1", Path("repo"), "standard", Path("git"), "objects",
        (detached_head,), "r1", None, "resolved",
    )
    assert occurrence.observed_refs == (detached_head,)


def test_markdown_explanations_reject_any_embedded_heading_or_trailing_newline() -> None:
    link = EvidenceLink("r1", "a" * 40, Path("src/a.py"), "symbol", "contribution")
    record = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40,), "yes", "yes", "problem", "technology", "contribution", "verification", "outcome", (), (link,), "refs", "d" * 64, "complete", "none")
    with pytest.raises(ValueError, match="Markdown section"):
        replace(record, problem="problem\n## Injected")
    with pytest.raises(ValueError, match="trailing newline"):
        replace(record, outcome="outcome\n")


def test_calibration_accepts_negative_kappa_for_failed_receipts() -> None:
    result = CalibrationResult("cal-negative", 24, 0.5, 0.5, -0.25, False)
    assert result.cohens_kappa == -0.25


def test_research_state_decision_matrix_and_transitions() -> None:
    repository = LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 1, "cataloged", "pending", "reason", None, (), "d" * 64)
    selected = replace(repository, research_status="selected", deep_research_decision="selected")
    assert repository.allows_transition_to(selected)
    assert selected.allows_transition_to(replace(selected, research_status="evidenced"))
    with pytest.raises(ValueError, match="status.*decision"):
        replace(repository, research_status="complete", deep_research_decision="excluded")
    assert not selected.allows_transition_to(replace(repository, research_status="unscanned"))

    states = {
        "unscanned": "pending",
        "cataloged": "pending",
        "selected": "selected",
        "excluded": "excluded",
        "evidenced": "selected",
        "reconciled": "selected",
        "complete": "selected",
    }
    allowed = {
        "unscanned": {"unscanned", "cataloged"},
        "cataloged": {"cataloged", "selected", "excluded"},
        "selected": {"selected", "evidenced"},
        "excluded": {"excluded"},
        "evidenced": {"evidenced", "reconciled"},
        "reconciled": {"reconciled", "complete"},
        "complete": {"complete", "selected"},
    }
    records = {
        status: replace(repository, research_status=status, deep_research_decision=decision)
        for status, decision in states.items()
    }
    for source_status, source in records.items():
        for target_status, target in records.items():
            assert source.allows_transition_to(target) == (target_status in allowed[source_status])
