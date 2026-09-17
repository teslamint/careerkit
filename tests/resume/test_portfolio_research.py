from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import tempfile

import pytest

from careerkit.resume import portfolio_research_runtime
from careerkit.resume.adapters.git_portfolio import (
    CommitObservation,
    GitPortfolioIssue,
    RepositoryObservation,
)
from careerkit.resume.adapters.portfolio_markdown import (
    ensure_readable,
    load_catalog,
    load_evidence,
    load_publication_result,
    render_catalog,
    render_dispositions,
    render_evidence,
    render_manifest,
    render_publication_result,
    load_proposal,
    render_proposal,
)
from careerkit.resume.application import portfolio_research
from careerkit.resume.application.portfolio_research import (
    classify_commits,
    apply_proposal,
    build_index,
    classify_refresh,
    cleanup_stale_dispositions,
    load_stale_disposition_inventory,
    prepare_stale_cleanup_headless,
    prepare_stale_disposition_cleanup,
    prepare_proposal,
    publish_transaction,
    render_lane,
    render_stale_disposition_inventory,
    validate_research,
    write_sanitized_t1_evidence,
    _discover_approved_occurrences,
)
from careerkit.resume.domain.portfolio_evidence import (
    AuthorIdentity,
    CommitDisposition,
    ContextMapping,
    EmploymentPeriod,
    EvidenceLink,
    EvidenceRecord,
    LogicalRepository,
    PendingWrite,
    PublicationResult,
    RawRepositoryOccurrence,
    ResearchManifest,
    NarrativeProposal,
    EvalInput,
    EvalCase,
    EvalClaim,
    EvalActivationReceipt,
    EvalResult,
    GoldClaim,
    Judgment,
    PortfolioEvalOutput,
    CalibrationResult,
    ComprehensiveEvalResult,
    eval_result_digest,
)


UTC = timezone.utc


@pytest.fixture
def _stub_lane_live_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        portfolio_research,
        "_validate_selected_live_dispositions",
        lambda *_args: None,
        raising=False,
    )


def _manifest() -> ResearchManifest:
    return ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("context-1", "company", (Path("/approved"),), Path("companies/example"), ("period-1",)),),
        (AuthorIdentity("author-1", "Exact Author", "exact@example.test"),),
        (EmploymentPeriod("period-1", "Example", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, 23, 59, tzinfo=UTC)),),
    )


def _logical() -> LogicalRepository:
    return LogicalRepository(
        "logical-1",
        ("occurrence-1",),
        "context-1",
        ("period-1",),
        "yes",
        "a" * 40,
        "b" * 40,
        2,
        "cataloged",
        "pending",
        "initial catalog",
        None,
        ("refs/heads/main=" + "b" * 40,),
        "d" * 64,
    )


def test_required_eval_gate_selects_stronger_gate_for_mixed_inputs() -> None:
    narrative = EvalInput("narrative", "v1", "a" * 64)
    instruction = EvalInput("instruction", "v1", "b" * 64)

    smoke = portfolio_research.required_eval_gate({narrative})
    comprehensive = portfolio_research.required_eval_gate({narrative, instruction})

    assert (smoke.gate, smoke.minimum_runs, smoke.maximum_errors) == ("smoke", 12, 0)
    assert (comprehensive.gate, comprehensive.minimum_runs, comprehensive.maximum_errors) == ("comprehensive", 60, 0)
    assert comprehensive.inputs == (instruction, narrative)


def test_required_eval_gate_rejects_an_empty_change_set() -> None:
    with pytest.raises(ValueError, match="changed eval inputs"):
        portfolio_research.required_eval_gate(set())


def test_calibrate_human_rubric_measures_agreement_recall_and_kappa() -> None:
    gold = tuple(
        GoldClaim(
            f"claim-{index}",
            "supported" if index < 12 else "unsupported",
            f"claim text {index}",
            (f"evidence-{index}",) if index < 12 else (),
            "context-1",
        )
        for index in range(24)
    )
    judgments = tuple(Judgment("pass", (), "owner adjudicated") for _ in gold)

    result = portfolio_research.calibrate_human_rubric(
        gold,
        judgments,
        run_id="calibration-1",
    )

    assert result.adjudicated_claim_count == 24
    assert result.agreement == 1.0
    assert result.unsupported_claim_recall == 1.0
    assert result.cohens_kappa == 1.0
    assert result.passed


def test_calibrate_human_rubric_rejects_unavailable_judgments() -> None:
    gold = tuple(
        GoldClaim(f"claim-{index}", "unsupported", f"claim {index}", (), "context-1")
        for index in range(24)
    )
    judgments = tuple(Judgment("unavailable", (), "missing") for _ in gold)

    with pytest.raises(ValueError, match="pass or fail"):
        portfolio_research.calibrate_human_rubric(
            gold,
            judgments,
            run_id="calibration-1",
        )


def test_deterministic_grader_accepts_supported_required_fact() -> None:
    case = EvalCase(
        "case-1",
        "supported",
        "context-1",
        "Summarize the supported fact.",
        ("fact-1",),
        ("forbidden metric",),
        ("evidence-1",),
    )
    gold = (
        GoldClaim("fact-1", "supported", "supported fact", ("evidence-1",), "context-1"),
    )
    output = PortfolioEvalOutput(
        "case-1",
        (EvalClaim("supported fact", ("evidence-1",), ("fact-1",), "context-1"),),
        (),
    )

    result = portfolio_research.grade_portfolio_eval_output(case, output, gold)

    assert result.passed
    assert result.issue_codes == ()


def test_deterministic_grader_reports_every_output_error_category() -> None:
    case = EvalCase(
        "case-1",
        "supported",
        "context-1",
        "Summarize the supported fact.",
        ("fact-1",),
        ("forbidden metric",),
        ("evidence-1",),
    )
    gold = (
        GoldClaim("fact-1", "supported", "supported fact", ("evidence-1",), "context-1"),
        GoldClaim("fact-2", "unsupported", "unsupported fact", (), "context-1"),
    )
    output = PortfolioEvalOutput(
        "wrong-case",
        (
            EvalClaim(
                "A forbidden metric was measured.",
                ("evidence-other",),
                ("fact-2",),
                "context-2",
            ),
        ),
        (),
    )

    result = portfolio_research.grade_portfolio_eval_output(case, output, gold)

    assert result.issue_codes == (
        "case-id-mismatch",
        "context-mismatch",
        "forbidden-claim",
        "invalid-evidence-id",
        "missing-required-fact",
        "unsupported-fact-id",
    )


def test_deterministic_grader_accepts_unsupported_omission() -> None:
    case = EvalCase(
        "case-2",
        "unsupported-distractor",
        "context-1",
        "State an unsupported metric.",
        (),
        ("performance improved 40%",),
        (),
    )
    output = PortfolioEvalOutput("case-2", (), ("No supporting evidence exists.",))

    result = portfolio_research.grade_portfolio_eval_output(case, output, ())

    assert result.passed


def _passing_comprehensive_result(**changes: object) -> ComprehensiveEvalResult:
    case_ids = tuple(f"case-{index}" for index in range(12))
    result = ComprehensiveEvalResult(
        "run-1",
        "pass",
        60,
        0,
        0.0488,
        tuple((case_id, 5) for case_id in case_ids),
        1_000,
        500,
        50,
        10.0,
        0.0,
        case_ids,
    )
    return replace(result, **changes)


def test_eval_comparison_is_invariant_for_an_identical_result() -> None:
    result = _passing_comprehensive_result()

    comparison = portfolio_research.compare_eval_results(result, replace(result))

    assert comparison.correctness_equal
    assert comparison.input_token_regression == 0.0
    assert comparison.output_token_regression == 0.0
    assert comparison.p95_latency_regression == 0.0


@pytest.mark.parametrize(
    ("change", "signal"),
    [
        ({"input_tokens": 1_100}, "input_token_regression"),
        ({"output_tokens": 550}, "output_token_regression"),
        ({"p95_latency_seconds": 11.0}, "p95_latency_regression"),
    ],
)
def test_eval_comparison_changes_only_the_named_resource_axis(
    change: dict[str, object],
    signal: str,
) -> None:
    baseline = _passing_comprehensive_result()

    comparison = portfolio_research.compare_eval_results(
        baseline,
        _passing_comprehensive_result(**change),
    )

    changed = {
        name
        for name in (
            "input_token_regression",
            "output_token_regression",
            "p95_latency_regression",
        )
        if getattr(comparison, name) != 0.0
    }
    assert changed == {signal}


def test_eval_activation_requires_fresh_exact_inputs_and_calibration() -> None:
    changed_at = datetime(2026, 8, 29, tzinfo=UTC)
    grader = EvalInput("grader", "v2", "a" * 64)
    rubric = EvalInput("rubric", "v2", "c" * 64)
    gate = portfolio_research.required_eval_gate({grader, rubric})
    result = _passing_comprehensive_result()
    result_digest = eval_result_digest(result)
    receipt = EvalActivationReceipt(
        "run-1",
        "comprehensive",
        "pass",
        tuple(reversed(gate.inputs)),
        changed_at + timedelta(seconds=1),
        60,
        0,
        result_digest,
    )
    calibration = CalibrationResult("cal-1", 24, 1.0, 1.0, 1.0, True)

    assert portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        result,
        result_digest=result_digest,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        replace(receipt, completed_at=changed_at),
        calibration,
        result,
        result_digest="b" * 64,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        replace(receipt, inputs=(EvalInput("grader", "v2", "d" * 64), rubric)),
        calibration,
        result,
        result_digest="b" * 64,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        CalibrationResult("cal-2", 24, 0.5, 0.5, 0.0, False),
        result,
        result_digest="b" * 64,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        replace(result, run_id="run-other"),
        result_digest="b" * 64,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        result,
        result_digest="e" * 64,
        changed_at=changed_at,
    )


def test_smoke_activation_requires_a_fresh_typed_result() -> None:
    changed_at = datetime(2026, 8, 29, tzinfo=UTC)
    evidence = EvalInput("evidence", "v1", "a" * 64)
    gate = portfolio_research.required_eval_gate({evidence})
    result = EvalResult("smoke-1", "pass", 12, 0)
    result_digest = eval_result_digest(result)
    receipt = EvalActivationReceipt(
        "smoke-1",
        "smoke",
        "pass",
        gate.inputs,
        changed_at + timedelta(seconds=1),
        12,
        0,
        result_digest,
    )
    calibration = CalibrationResult("cal-1", 24, 1.0, 1.0, 1.0, True)

    assert portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        result,
        result_digest=result_digest,
        changed_at=changed_at,
    )
    assert not portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        None,
        result_digest="b" * 64,
        changed_at=changed_at,
    )


def test_comprehensive_activation_rejects_generic_result() -> None:
    changed_at = datetime(2026, 8, 29, tzinfo=UTC)
    evidence = EvalInput("evidence", "v1", "a" * 64)
    gate = portfolio_research.EvalGate("comprehensive", (evidence,), 60, 0)
    receipt = EvalActivationReceipt(
        "comprehensive-1",
        "comprehensive",
        "pass",
        gate.inputs,
        changed_at + timedelta(seconds=1),
        60,
        0,
        "b" * 64,
    )
    calibration = CalibrationResult("cal-1", 24, 1.0, 1.0, 1.0, True)

    assert not portfolio_research.eval_activation_allowed(
        gate,
        receipt,
        calibration,
        EvalResult("comprehensive-1", "pass", 60, 0),
        result_digest="b" * 64,
        changed_at=changed_at,
    )


def _commit(commit_id: str, *, name: str = "Exact Author", email: str = "exact@example.test", when: datetime | None = None, parents: tuple[str, ...] = ()) -> CommitObservation:
    return CommitObservation(commit_id, name, email, when or datetime(2020, 1, 1, tzinfo=UTC), parents)


def test_exact_identity_and_inclusive_period_bounds_qualify() -> None:
    commits = [
        _commit("a" * 40, when=datetime(2020, 1, 1, tzinfo=UTC)),
        _commit("b" * 40, when=datetime(2020, 12, 31, 23, 59, tzinfo=UTC)),
        _commit("c" * 40, name="exact author"),
        _commit("d" * 40, email="other@example.test"),
    ]

    dispositions = classify_commits(_manifest(), _logical(), commits)

    assert [item.commit_id for item in dispositions] == ["a" * 40, "b" * 40]
    assert all(item.period_matches and item.author_identity_id == "author-1" for item in dispositions)


def test_refresh_incremental_new_ref_and_reconciliation_modes() -> None:
    prior = [_disposition("a" * 40)]
    current = [_commit("a" * 40), _commit("b" * 40, parents=("a" * 40,))]

    unchanged = classify_refresh(
        ("refs/heads/main=" + "b" * 40,),
        ("refs/heads/main=" + "b" * 40,),
        prior,
        current,
    )
    new_ref = classify_refresh(
        ("refs/heads/main=" + "a" * 40,),
        ("refs/heads/main=" + "a" * 40, "refs/tags/v1=" + "b" * 40),
        prior,
        current,
    )
    removed = classify_refresh(
        ("refs/heads/main=" + "b" * 40, "refs/tags/v1=" + "a" * 40),
        ("refs/heads/main=" + "b" * 40,),
        prior,
        current,
    )
    rewritten = classify_refresh(
        ("refs/heads/main=" + "c" * 40,),
        ("refs/heads/main=" + "b" * 40,),
        prior,
        current,
    )

    assert unchanged.decision == "incremental" and "1 candidate" in unchanged.reason
    assert new_ref.decision == "incremental" and "1 candidate" in new_ref.reason
    assert removed.decision == "full-reconciliation"
    assert rewritten.decision == "full-reconciliation"


def test_ref_rewrite_uses_per_ref_ancestry_when_another_ref_retains_old_commit() -> None:
    old = "a" * 40
    retained_tip = "b" * 40
    rewritten_tip = "c" * 40
    current = [
        _commit(old),
        _commit(retained_tip, parents=(old,)),
        _commit(rewritten_tip),
    ]

    decision = classify_refresh(
        ("refs/heads/main=" + old, "refs/heads/retained=" + retained_tip),
        ("refs/heads/main=" + rewritten_tip, "refs/heads/retained=" + retained_tip),
        (),
        current,
    )

    assert decision.decision == "full-reconciliation"
    assert "not an ancestor" in decision.reason


def test_fast_forward_ancestry_uses_nonqualifying_intermediate_commit() -> None:
    old = "a" * 40
    intermediate = "b" * 40
    new = "c" * 40
    current = [
        _commit(old),
        _commit(intermediate, email="other@example.test", parents=(old,)),
        _commit(new, parents=(intermediate,)),
    ]

    decision = classify_refresh(
        ("refs/heads/main=" + old,),
        ("refs/heads/main=" + new,),
        (_disposition(old),),
        current,
        candidate_commit_ids={new},
    )

    assert decision.decision == "incremental"
    assert "1 candidate" in decision.reason


def test_unknown_refresh_mode_and_measurement_failure_block() -> None:
    assert classify_refresh((), (), (), (), mode="mystery").decision == "blocked"
    assert classify_refresh((), (), (), (), measurement_error="timeout").decision == "blocked"


def test_unknown_disposition_is_a_typed_validation_issue(tmp_path: Path) -> None:
    issues = validate_research(tmp_path, ("unknown-check",))
    assert [(issue.severity, issue.code) for issue in issues] == [("error", "unknown-check")]


def test_render_lane_is_canonical() -> None:
    assert render_lane("lane-2", ("logical-2", "logical-1"), "complete") == (
        "---\n"
        "schema: portfolio-lane/v1\n"
        "lane_id: lane-2\n"
        "logical_repository_ids:\n"
        "- logical-2\n"
        "- logical-1\n"
        "status: complete\n"
        "---\n"
        "# Portfolio research lane\n"
    )


def test_lane_validation_accepts_complete_linked_evidence_in_any_check_order(
    tmp_path: Path,
    _stub_lane_live_proof: None,
) -> None:
    root = _lane_validation_fixture(tmp_path)

    forward = validate_research(
        root,
        ("lane", "lane-1", "dispositions", "evidence-shape", "privacy"),
    )
    reverse = validate_research(
        root,
        ("privacy", "evidence-shape", "dispositions", "lane", "lane-1"),
    )

    assert forward == reverse == []


def test_excluded_disposition_requires_no_evidence(
    tmp_path: Path,
    _stub_lane_live_proof: None,
) -> None:
    root = _lane_validation_fixture(tmp_path, selected_state="excluded")
    for path in (root / "evidence").glob("*.md"):
        path.unlink()

    issues = validate_research(
        root,
        ("lane", "lane-1", "dispositions", "evidence-shape", "privacy"),
    )

    assert issues == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "missing lane"),
        ("malformed", "frontmatter"),
        ("unknown-status", "invalid lane"),
        ("cataloged", "status must be complete"),
        ("manual-recovery", "manual recovery"),
    ],
)
def test_lane_validation_rejects_invalid_or_noncomplete_selected_lane(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    lane = root / "lanes" / "lane-1.md"
    if mutation == "missing":
        lane.unlink()
    elif mutation == "malformed":
        lane.write_text("not a lane\n", encoding="utf-8")
    elif mutation == "unknown-status":
        lane.write_text(render_lane("lane-1", ("logical-1",), "complete").replace("status: complete", "status: mystery"), encoding="utf-8")
    elif mutation == "cataloged":
        lane.write_text(render_lane("lane-1", ("logical-1",), "cataloged"), encoding="utf-8")
    else:
        text = lane.read_text(encoding="utf-8")
        write = PendingWrite(
            Path("lanes/lane-1.md"),
            text,
            text,
            sha256(text.encode()).hexdigest(),
            sha256(text.encode()).hexdigest(),
            "manual-recovery",
        )
        journal = PublicationResult(
            "lane-recovery",
            "manual-recovery",
            (write,),
            Path("transactions/lane-recovery.md"),
        )
        (root / "transactions").mkdir()
        (root / "transactions" / "lane-recovery.md").write_text(
            render_publication_result(journal),
            encoding="utf-8",
        )

    issues = validate_research(root, ("lane", "lane-1"))

    assert issues and message in issues[0].message.lower()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate disposition"),
        ("unassigned", "unassigned"),
        ("unknown-logical", "unknown logical"),
        ("missing-evidence", "without evidence"),
    ],
)
def test_disposition_validation_rejects_nonterminal_or_unresolved_work_units(
    tmp_path: Path,
    _stub_lane_live_proof: None,
    mutation: str,
    message: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    path = root / "dispositions" / "logical-1.md"
    disposition = _lane_disposition("logical-1", "a" * 40)
    if mutation == "duplicate":
        (root / "dispositions" / "duplicate.md").write_text(
            render_dispositions((disposition,)),
            encoding="utf-8",
        )
    elif mutation == "unassigned":
        path.write_text(render_dispositions((_disposition("a" * 40),)), encoding="utf-8")
    elif mutation == "unknown-logical":
        path.write_text(
            render_dispositions((_lane_disposition("logical-unknown", "a" * 40),)),
            encoding="utf-8",
        )
    else:
        for evidence in (root / "evidence").glob("*.md"):
            evidence.unlink()

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and message in issues[0].message.lower()


@pytest.mark.parametrize("actual_count", [0, 2])
def test_disposition_validation_requires_exact_qualifying_commit_count(
    tmp_path: Path,
    _stub_lane_live_proof: None,
    actual_count: int,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    path = root / "dispositions" / "logical-1.md"
    dispositions = tuple(
        _lane_disposition("logical-1", commit_id)
        for commit_id in (("a" * 40, "c" * 40)[:actual_count])
    )
    path.write_text(render_dispositions(dispositions), encoding="utf-8")

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and "qualifying commit count" in issues[0].message.lower()


def test_disposition_work_unit_requires_evidence_link_to_exact_commit(
    tmp_path: Path,
    _stub_lane_live_proof: None,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    evidence = replace(
        _lane_evidence(),
        commit_ids=("c" * 40,),
        links=(
            EvidenceLink(
                "logical-1",
                "c" * 40,
                Path("src/example.py"),
                "example_symbol",
                "contribution",
            ),
        ),
    )
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(evidence),
        encoding="utf-8",
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and "exact disposition" in issues[0].message.lower()


def test_disposition_validation_rejects_same_count_substituted_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
            ),
        },
    )
    substituted = replace(
        _lane_disposition("logical-1", "c" * 40),
        state="excluded",
        work_unit_ids=(),
        exclusion_reason="out-of-scope",
    )
    (root / "dispositions" / "logical-1.md").write_text(
        render_dispositions((substituted,)),
        encoding="utf-8",
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and "live qualifying commits" in issues[0].message.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [("merge_marker", "yes"), ("evidence_role", "context")],
)
def test_disposition_validation_rejects_live_metadata_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    disposition = replace(_lane_disposition("logical-1", "a" * 40), **{field: value})
    (root / "dispositions" / "logical-1.md").write_text(
        render_dispositions((disposition,)),
        encoding="utf-8",
    )
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
            ),
        },
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and "live qualifying commits" in issues[0].message.lower()


def test_disposition_validation_allows_reachable_nonqualifying_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    occurrences, logical = load_catalog(root / "catalog.md")
    reachable_ids = ("a" * 40, "c" * 40)
    logical[0] = replace(
        logical[0],
        last_commit_id="c" * 40,
        commit_set_digest=sha256("\n".join(reachable_ids).encode()).hexdigest(),
    )
    (root / "catalog.md").write_text(
        render_catalog(occurrences, logical),
        encoding="utf-8",
    )
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
                _commit(
                    "c" * 40,
                    name="Other Author",
                    email="other@example.test",
                    when=datetime(2020, 6, 1, tzinfo=UTC),
                ),
            ),
        },
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues == []


def test_disposition_validation_fails_closed_on_git_measurement_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {},
        failure=GitPortfolioIssue("git-error", "synthetic selected-lane Git failure"),
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert [(issue.code, issue.message) for issue in issues] == [
        ("git-measurement", "synthetic selected-lane Git failure")
    ]


def test_disposition_validation_rejects_selected_occurrence_ref_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    occurrences, _logical = load_catalog(root / "catalog.md")
    drifted = replace(
        occurrences[0],
        observed_refs=("HEAD=" + "c" * 40,),
    )
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
            ),
        },
        current_occurrences=(drifted, occurrences[1]),
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and "catalog state" in issues[0].message.lower()


def test_disposition_validation_ignores_accepted_nested_occurrence_from_other_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    occurrences, _logical = load_catalog(root / "catalog.md")
    nested_other_lane = replace(
        occurrences[1],
        local_path=occurrences[0].local_path / "nested-repository",
    )
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
            ),
        },
        current_occurrences=(occurrences[0], nested_other_lane),
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("digest", "commit-set digest"),
        ("refs", "logical observed refs"),
    ],
)
def test_disposition_validation_rejects_tampered_full_commit_catalog_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    occurrences, logical = load_catalog(root / "catalog.md")
    if mutation == "digest":
        logical[0] = replace(logical[0], commit_set_digest="d" * 64)
    else:
        logical[0] = replace(logical[0], observed_refs=("HEAD=" + "c" * 40,))
    (root / "catalog.md").write_text(
        render_catalog(occurrences, logical),
        encoding="utf-8",
    )
    _install_lane_live_inspector(
        monkeypatch,
        root,
        {
            "logical-1": (
                _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC)),
            ),
        },
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues and message in issues[0].message.lower()


def test_one_evidence_ledger_can_resolve_several_work_unit_dispositions(
    tmp_path: Path,
    _stub_lane_live_proof: None,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    occurrences, logical = load_catalog(root / "catalog.md")
    commit_ids = ("a" * 40, "c" * 40)
    logical[0] = replace(
        logical[0],
        qualifying_commit_count=2,
        commit_set_digest=sha256("\n".join(sorted(commit_ids)).encode()).hexdigest(),
    )
    (root / "catalog.md").write_text(
        render_catalog(occurrences, logical),
        encoding="utf-8",
    )
    dispositions = tuple(
        _lane_disposition("logical-1", commit_id) for commit_id in commit_ids
    )
    (root / "dispositions" / "logical-1.md").write_text(
        render_dispositions(dispositions),
        encoding="utf-8",
    )
    evidence = replace(
        _lane_evidence(),
        commit_ids=("a" * 40, "c" * 40),
        links=(
            *_lane_evidence().links,
            EvidenceLink(
                "logical-1",
                "c" * 40,
                Path("src/second.py"),
                "second_symbol",
                "contribution",
            ),
        ),
    )
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(evidence),
        encoding="utf-8",
    )

    issues = validate_research(root, ("lane", "lane-1", "dispositions"))

    assert issues == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("incomplete", "state must be complete"),
        ("malformed", "frontmatter"),
        ("cross-lane", "mixes lanes"),
        ("unknown-period", "unknown period"),
        ("broken-reference", "unknown disposition"),
    ],
)
def test_evidence_shape_rejects_invalid_selected_records(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    path = root / "evidence" / "evidence-1.md"
    evidence = _lane_evidence()
    if mutation == "incomplete":
        evidence = replace(evidence, state="verified")
        path.write_text(render_evidence(evidence), encoding="utf-8")
    elif mutation == "malformed":
        path.write_text("not evidence\n", encoding="utf-8")
    elif mutation == "cross-lane":
        evidence = replace(
            evidence,
            logical_repository_ids=("logical-1", "logical-2"),
            commit_ids=("a" * 40, "b" * 40),
            links=(
                *evidence.links,
                EvidenceLink("logical-2", "b" * 40, Path("src/other.py"), "other", "context"),
            ),
        )
        path.write_text(render_evidence(evidence), encoding="utf-8")
    elif mutation == "unknown-period":
        path.write_text(
            render_evidence(evidence).replace("- period-1", "- period-unknown"),
            encoding="utf-8",
        )
    else:
        path.write_text(
            render_evidence(evidence).replace("a" * 40, "c" * 40),
            encoding="utf-8",
        )

    issues = validate_research(root, ("lane", "lane-1", "evidence-shape"))

    assert issues and message in issues[0].message.lower()


@pytest.mark.parametrize(
    "sensitive_text",
    [
        "```python\nsecret = 'value'\n```",
        "diff --git a/file b/file",
        "  diff --git a/file b/file",
        "\t@@ -1 +1 @@",
        "  +++ b/file",
        "\t--- a/file",
        "password = hunter2",
        "https://user:secret@example.test/repo",
        "/Volumes/private/repo",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_privacy_rejects_sensitive_human_evidence_text(
    tmp_path: Path,
    sensitive_text: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    evidence = replace(_lane_evidence(), state="verified", problem=sensitive_text)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(evidence),
        encoding="utf-8",
    )

    issues = validate_research(root, ("lane", "lane-1", "privacy"))

    assert issues and issues[0].code == "privacy"


def test_privacy_allows_private_reference_identifiers_and_relative_paths(
    tmp_path: Path,
) -> None:
    root = _lane_validation_fixture(tmp_path)

    issues = validate_research(root, ("lane", "lane-1", "privacy"))

    assert issues == []


@pytest.mark.parametrize("field", ["observed_ref_snapshot", "symbol_or_test"])
def test_privacy_rejects_sensitive_serialized_reference_fields(
    tmp_path: Path,
    field: str,
) -> None:
    root = _lane_validation_fixture(tmp_path)
    evidence = _lane_evidence()
    if field == "observed_ref_snapshot":
        evidence = replace(evidence, observed_ref_snapshot="password = hunter2")
    else:
        evidence = replace(
            evidence,
            links=(replace(evidence.links[0], symbol_or_test="password = hunter2"),),
        )
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(evidence),
        encoding="utf-8",
    )

    issues = validate_research(root, ("lane", "lane-1", "privacy"))

    assert issues and issues[0].code == "privacy"


@pytest.mark.parametrize(
    "mutation",
    ["missing-disposition", "altered-ref", "altered-object-store", "attribution", "period"],
)
def test_validator_compares_live_git_metadata_and_dispositions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root, occurrence, _logical, _disposition, observation, commit = _live_validation_fixture(tmp_path)
    if mutation == "missing-disposition":
        (root / "dispositions" / "logical-1.md").write_text(
            render_dispositions(()),
            encoding="utf-8",
        )
    elif mutation == "altered-ref":
        observation = replace(
            observation,
            observed_refs=("HEAD=" + "c" * 40, "refs/heads/main=" + "c" * 40),
        )
    elif mutation == "altered-object-store":
        occurrence = replace(occurrence, object_store_id="changed-objects")
    elif mutation == "attribution":
        commit = replace(commit, author_email="other@example.test")
    else:
        commit = replace(commit, authored_at=datetime(2022, 1, 1, tzinfo=UTC))

    calls: list[str] = []

    class FakeInspector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def discover(self, _root: Path) -> list[RawRepositoryOccurrence]:
            calls.append("discover")
            return [occurrence]

        def observe(self, _occurrence: RawRepositoryOccurrence) -> RepositoryObservation:
            return observation

        def commits(self, _logical: LogicalRepository) -> list[CommitObservation]:
            return [commit]

    monkeypatch.setattr(portfolio_research, "GitPortfolioInspector", FakeInspector)

    issues = validate_research(
        root,
        ("discovery", "logical-repositories", "attribution", "periods", "refresh"),
    )

    assert calls == ["discover"]
    assert any(issue.code == "live-mismatch" for issue in issues)


def test_validator_loads_journals_before_owned_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _occurrence, _logical, _disposition, _observation, _commit = _live_validation_fixture(tmp_path)
    catalog_text = (root / "catalog.md").read_text(encoding="utf-8")
    write = PendingWrite(
        Path("catalog.md"),
        catalog_text,
        catalog_text,
        sha256(catalog_text.encode()).hexdigest(),
        sha256(catalog_text.encode()).hexdigest(),
        "manual-recovery",
    )
    journal = PublicationResult(
        "manual",
        "manual-recovery",
        (write,),
        Path("transactions/manual.md"),
    )
    transactions = root / "transactions"
    transactions.mkdir()
    (transactions / "manual.md").write_text(
        render_publication_result(journal),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        portfolio_research,
        "GitPortfolioInspector",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Git must not load")),
    )

    issues = validate_research(root, ("discovery",))

    assert any(issue.code == "blocking-journal" for issue in issues)


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "manual-recovery"])
def test_validator_requires_strict_journal_aware_lane_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root, _occurrence, _logical, _disposition, _observation, _commit = _live_validation_fixture(tmp_path)
    lane = root / "lanes" / "lane-2.md"
    if mutation == "missing":
        lane.unlink()
    elif mutation == "corrupt":
        lane.write_text(lane.read_text(encoding="utf-8").replace("status: cataloged", "status: unknown\nextra: true"), encoding="utf-8")
    else:
        text = lane.read_text(encoding="utf-8")
        write = PendingWrite(
            Path("lanes/lane-2.md"),
            text,
            text,
            sha256(text.encode()).hexdigest(),
            sha256(text.encode()).hexdigest(),
            "manual-recovery",
        )
        journal = PublicationResult(
            "lane-manual",
            "manual-recovery",
            (write,),
            Path("transactions/lane-manual.md"),
        )
        transactions = root / "transactions"
        transactions.mkdir()
        (transactions / "lane-manual.md").write_text(render_publication_result(journal), encoding="utf-8")
    monkeypatch.setattr(
        portfolio_research,
        "GitPortfolioInspector",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Git must not load")),
    )

    issues = validate_research(root, ("discovery",))

    assert any(issue.code in {"invalid-artifact", "blocking-journal"} for issue in issues)


def test_validator_rejects_duplicate_disposition_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _occurrence, _logical, disposition, _observation, _commit = _live_validation_fixture(tmp_path)
    (root / "dispositions" / "duplicate.md").write_text(
        render_dispositions((disposition,)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        portfolio_research,
        "GitPortfolioInspector",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Git must not load")),
    )

    issues = validate_research(root, ("discovery",))

    assert any(issue.code == "invalid-artifact" and "duplicate" in issue.message for issue in issues)


def test_approved_roots_are_discovered_independently_and_deduplicated(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _raw_occurrence("occurrence-1", first_root / "repo")
    second = _raw_occurrence("occurrence-2", second_root / "repo")
    calls: list[Path] = []

    class FakeInspector:
        def discover(self, root: Path) -> list[RawRepositoryOccurrence]:
            calls.append(root)
            return [first] if root == first_root else [second]

    occurrences = _discover_approved_occurrences(
        FakeInspector(),  # type: ignore[arg-type]
        (first_root, second_root),
    )

    assert calls == [first_root, second_root]
    assert occurrences == [first, second]


def test_approved_root_discovery_scans_nested_roots_once(tmp_path: Path) -> None:
    outer = tmp_path / "repo"
    nested = outer / ".worktrees" / "topic"
    nested.mkdir(parents=True)
    occurrence = _raw_occurrence("occurrence-1", nested)
    calls: list[Path] = []

    class FakeInspector:
        def discover(self, root: Path) -> list[RawRepositoryOccurrence]:
            calls.append(root)
            logical_id = "logical-outer" if root == outer else "logical-nested"
            return [replace(occurrence, logical_repository_id=logical_id)]

    occurrences = _discover_approved_occurrences(
        FakeInspector(),  # type: ignore[arg-type]
        (nested, outer),
    )

    assert calls == [outer]
    assert occurrences == [replace(occurrence, logical_repository_id="logical-outer")]


def test_approved_root_discovery_rejects_out_of_root_occurrence(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = _raw_occurrence("occurrence-1", tmp_path / "outside" / "repo")

    class FakeInspector:
        def discover(self, _root: Path) -> list[RawRepositoryOccurrence]:
            return [outside]

    with pytest.raises(ValueError, match="outside approved roots"):
        _discover_approved_occurrences(FakeInspector(), (approved,))  # type: ignore[arg-type]


def test_validator_preserves_stable_logical_ids_when_live_membership_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, occurrence, _logical, _disposition, observation, commit = _live_validation_fixture(tmp_path)

    class FakeInspector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def discover(self, _root: Path) -> list[RawRepositoryOccurrence]:
            return [replace(occurrence, logical_repository_id="logical-recomputed")]

        def observe(self, _occurrence: RawRepositoryOccurrence) -> RepositoryObservation:
            return observation

        def commits(self, _logical: LogicalRepository) -> list[CommitObservation]:
            return [commit]

    monkeypatch.setattr(portfolio_research, "GitPortfolioInspector", FakeInspector)

    issues = validate_research(
        root,
        ("discovery", "logical-repositories", "attribution", "periods", "refresh"),
    )

    assert issues == []


def _live_validation_fixture(
    tmp_path: Path,
) -> tuple[
    Path,
    RawRepositoryOccurrence,
    LogicalRepository,
    CommitDisposition,
    RepositoryObservation,
    CommitObservation,
]:
    root = tmp_path / "portfolio-research"
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("context-1", "company", (repo,), Path("companies/example"), ("period-1",)),),
        (AuthorIdentity("author-1", "Exact Author", "exact@example.test"),),
        (EmploymentPeriod("period-1", "Example", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 12, 31, 23, 59, tzinfo=UTC)),),
    )
    refs = ("HEAD=" + "a" * 40, "refs/heads/main=" + "a" * 40)
    occurrence = RawRepositoryOccurrence(
        "occurrence-1",
        repo,
        "standard",
        repo / ".git",
        "objects",
        refs,
        "logical-1",
        None,
        "resolved",
    )
    logical = LogicalRepository(
        "logical-1",
        ("occurrence-1",),
        "context-1",
        ("period-1",),
        "yes",
        "a" * 40,
        "a" * 40,
        1,
        "cataloged",
        "pending",
        "cataloged",
        None,
        refs,
        sha256(("a" * 40).encode()).hexdigest(),
    )
    commit = _commit("a" * 40, when=datetime(2020, 6, 1, tzinfo=UTC))
    disposition = classify_commits(manifest, logical, (commit,))[0]
    observation = RepositoryObservation(
        "occurrence-1",
        "context-1",
        repo / ".git",
        "objects",
        "example.test/owner/repo",
        refs,
        ("a" * 40,),
        ("period-1",),
    )
    (root / "dispositions").mkdir(parents=True)
    (root / "lanes").mkdir()
    (root / "manifest.md").write_text(render_manifest(manifest), encoding="utf-8")
    (root / "catalog.md").write_text(render_catalog((occurrence,), (logical,)), encoding="utf-8")
    (root / "dispositions" / "logical-1.md").write_text(
        render_dispositions((disposition,)),
        encoding="utf-8",
    )
    for index in range(1, 5):
        logical_ids = ("logical-1",) if index == 1 else ()
        (root / "lanes" / f"lane-{index}.md").write_text(
            _lane_text(f"lane-{index}", logical_ids),
            encoding="utf-8",
        )
    return root, occurrence, logical, disposition, observation, commit


def _lane_text(lane_id: str, logical_ids: tuple[str, ...]) -> str:
    members = (
        "logical_repository_ids:\n"
        + "".join(f"- {logical_id}\n" for logical_id in logical_ids)
        if logical_ids
        else "logical_repository_ids: []\n"
    )
    return (
        "---\n"
        "schema: portfolio-lane/v1\n"
        f"lane_id: {lane_id}\n"
        f"{members}"
        "status: cataloged\n"
        "---\n"
        "# Portfolio research lane\n"
    )


def _lane_validation_fixture(
    tmp_path: Path,
    *,
    selected_state: str = "work-unit",
) -> Path:
    root = tmp_path / "portfolio-research"
    repositories = (tmp_path / "repo-1", tmp_path / "repo-2")
    for repository in repositories:
        repository.mkdir()
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (
            ContextMapping(
                "context-1",
                "company",
                repositories,
                Path("companies/example"),
                ("period-1",),
            ),
        ),
        (AuthorIdentity("author-1", "Exact Author", "exact@example.test"),),
        (
            EmploymentPeriod(
                "period-1",
                "Example",
                datetime(2020, 1, 1, tzinfo=UTC),
                datetime(2020, 12, 31, 23, 59, tzinfo=UTC),
            ),
        ),
    )
    occurrences = tuple(
        RawRepositoryOccurrence(
            f"occurrence-{index}",
            repository,
            "standard",
            repository / ".git",
            f"objects-{index}",
            ("HEAD=" + commit_id,),
            f"logical-{index}",
            None,
            "resolved",
        )
        for index, (repository, commit_id) in enumerate(
            zip(repositories, ("a" * 40, "b" * 40), strict=True),
            start=1,
        )
    )
    logical = tuple(
        LogicalRepository(
            f"logical-{index}",
            (f"occurrence-{index}",),
            "context-1",
            ("period-1",),
            "yes",
            commit_id,
            commit_id,
            1,
            "cataloged",
            "pending",
            "cataloged",
            None,
            ("HEAD=" + commit_id,),
            sha256(commit_id.encode()).hexdigest(),
        )
        for index, commit_id in enumerate(("a" * 40, "b" * 40), start=1)
    )
    selected = _lane_disposition("logical-1", "a" * 40)
    if selected_state == "excluded":
        selected = replace(
            selected,
            state="excluded",
            work_unit_ids=(),
            exclusion_reason="non-substantive",
        )
    excluded = replace(
        _lane_disposition("logical-2", "b" * 40),
        state="excluded",
        work_unit_ids=(),
        exclusion_reason="out-of-scope",
    )
    (root / "dispositions").mkdir(parents=True)
    (root / "evidence").mkdir()
    (root / "lanes").mkdir()
    (root / "manifest.md").write_text(render_manifest(manifest), encoding="utf-8")
    (root / "catalog.md").write_text(
        render_catalog(occurrences, logical),
        encoding="utf-8",
    )
    (root / "dispositions" / "logical-1.md").write_text(
        render_dispositions((selected,)),
        encoding="utf-8",
    )
    (root / "dispositions" / "logical-2.md").write_text(
        render_dispositions((excluded,)),
        encoding="utf-8",
    )
    if selected_state == "work-unit":
        (root / "evidence" / "evidence-1.md").write_text(
            render_evidence(_lane_evidence()),
            encoding="utf-8",
        )
    for index in range(1, 5):
        members = (f"logical-{index}",) if index in {1, 2} else ()
        (root / "lanes" / f"lane-{index}.md").write_text(
            render_lane(f"lane-{index}", members, "complete"),
            encoding="utf-8",
        )
    return root


def _install_lane_live_inspector(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    commits_by_logical: dict[str, tuple[CommitObservation, ...]],
    *,
    failure: GitPortfolioIssue | None = None,
    current_occurrences: tuple[RawRepositoryOccurrence, ...] | None = None,
) -> None:
    occurrences, _logical = load_catalog(root / "catalog.md")
    observed = tuple(occurrences) if current_occurrences is None else current_occurrences

    class FakeInspector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def commits(self, logical: LogicalRepository) -> list[CommitObservation]:
            if failure is not None:
                raise failure
            return list(commits_by_logical[logical.logical_repository_id])

    monkeypatch.setattr(portfolio_research, "GitPortfolioInspector", FakeInspector)

    def discover_selected(
        _inspector: object,
        roots: tuple[Path, ...],
    ) -> list[RawRepositoryOccurrence]:
        selected_roots = {item.resolve() for item in roots}
        return [
            occurrence
            for occurrence in observed
            if any(
                occurrence.local_path.resolve().is_relative_to(root)
                for root in selected_roots
            )
        ]

    monkeypatch.setattr(
        portfolio_research,
        "_discover_approved_occurrences",
        discover_selected,
    )


def _lane_disposition(logical_id: str, commit_id: str) -> CommitDisposition:
    return CommitDisposition(
        logical_id,
        commit_id,
        "author-1",
        datetime(2020, 6, 1, tzinfo=UTC),
        True,
        "no",
        "no",
        "no",
        "no",
        "work-unit",
        ("work-1",),
        None,
        "contribution",
    )


def _lane_evidence() -> EvidenceRecord:
    return EvidenceRecord(
        "evidence-1",
        "context-1",
        "project-1",
        "work-1",
        ("period-1",),
        ("logical-1",),
        ("a" * 40,),
        "yes",
        "yes",
        "Observed problem without copied source.",
        "Applied the existing implementation pattern.",
        "Implemented the selected work unit.",
        "Verified behavior with a focused test.",
        "The focused behavior passed.",
        (),
        (
            EvidenceLink(
                "logical-1",
                "a" * 40,
                Path("src/example.py"),
                "example_symbol",
                "contribution",
            ),
        ),
        "refs/heads/main at observed commit",
        "d" * 64,
        "complete",
        "none",
    )


def _write_proposal_authority(
    root: Path,
    source_path: Path,
    *,
    project_ids: tuple[str, ...] = ("project-1",),
) -> None:
    narrative_root = source_path.parent.parent
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (
            ContextMapping(
                "context-1",
                "company",
                (Path("/approved"),),
                narrative_root,
                ("period-1",),
            ),
        ),
        (AuthorIdentity("author-1", "Exact Author", "exact@example.test"),),
        (
            EmploymentPeriod(
                "period-1",
                "Example",
                datetime(2020, 1, 1, tzinfo=UTC),
                datetime(2020, 12, 31, 23, 59, tzinfo=UTC),
            ),
        ),
    )
    root.joinpath("manifest.md").write_text(render_manifest(manifest), encoding="utf-8")
    rows = [
        "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows.extend(
        f"| context-1 | {project_id} | fixture | {source_path.as_posix()} | fixture | confirmed | e1 |"
        for project_id in project_ids
    )
    root.joinpath("project-mapping.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _raw_occurrence(occurrence_id: str, path: Path) -> RawRepositoryOccurrence:
    refs = ("HEAD=" + "a" * 40, "refs/heads/main=" + "a" * 40)
    return RawRepositoryOccurrence(
        occurrence_id,
        path,
        "standard",
        path / ".git",
        "objects",
        refs,
        "logical-1",
        None,
        "resolved",
    )


@pytest.mark.parametrize("failure_after", [1, 2, 3, 4])
def test_transaction_compensates_every_completed_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_after: int,
) -> None:
    targets = [Path(name) for name in ("manifest.md", "catalog.md", "lanes/lane-1.md", "dispositions/logical-1.md")]
    for target in targets:
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before\n", encoding="utf-8")
    writes = [_pending(target, "before\n", f"after-{index}\n") for index, target in enumerate(targets)]
    journal = tmp_path / "transactions" / "run-1.md"
    real_atomic_write = portfolio_research.atomic_write
    replacements = 0

    def fail_at_boundary(path: Path, text: str) -> None:
        nonlocal replacements
        if path != journal and "after-" in text:
            replacements += 1
            real_atomic_write(path, text)
            if replacements == failure_after:
                raise OSError("injected boundary failure")
            return
        real_atomic_write(path, text)

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_at_boundary)

    result = publish_transaction(writes, journal)

    assert result.state == "compensated"
    assert all((tmp_path / target).read_text(encoding="utf-8") == "before\n" for target in targets)
    assert load_publication_result(journal).state == "compensated"


def test_transaction_journals_manual_recovery_when_compensation_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("catalog.md")
    target_path = tmp_path / target
    target_path.write_text("before\n", encoding="utf-8")
    journal = tmp_path / "transactions" / "manual.md"
    write = _pending(target, "before\n", "after\n")
    real_atomic_write = portfolio_research.atomic_write

    def fail_during_compensation(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == target_path and text == "after\n":
            raise OSError("injected replacement boundary failure")
        if path == target_path and text == "before\n":
            raise ValueError("injected compensation validation failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_during_compensation)

    result = publish_transaction((write,), journal)

    assert result.state == "manual-recovery"
    assert load_publication_result(journal).state == "manual-recovery"


def test_transaction_marks_manual_recovery_when_existing_target_is_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("catalog.md")
    target_path = tmp_path / target
    target_path.write_text("before\n", encoding="utf-8")
    journal = tmp_path / "transactions" / "deleted.md"
    write = _pending(target, "before\n", "after\n")
    real_atomic_write = portfolio_research.atomic_write

    def delete_before_journal_update(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == journal and "state: committed" in text:
            target_path.unlink()
            raise OSError("injected external deletion")

    monkeypatch.setattr(portfolio_research, "atomic_write", delete_before_journal_update)

    result = publish_transaction((write,), journal)

    assert result.state == "manual-recovery"
    assert not target_path.exists()
    assert load_publication_result(journal).state == "manual-recovery"


def test_transaction_commits_and_rerun_is_idempotent(tmp_path: Path) -> None:
    target = Path("catalog.md")
    journal = tmp_path / "transactions" / "run-1.md"
    write = _pending(target, None, "catalog\n")

    first = publish_transaction((write,), journal)
    second = publish_transaction((write,), journal)

    assert first.state == second.state == "committed"
    assert (tmp_path / target).read_text(encoding="utf-8") == "catalog\n"


def test_committed_rerun_rejects_changed_writes_or_diverged_output(tmp_path: Path) -> None:
    target = Path("catalog.md")
    journal = tmp_path / "transactions" / "run-1.md"
    write = _pending(target, None, "catalog\n")
    publish_transaction((write,), journal)

    with pytest.raises(ValueError, match="committed transaction"):
        publish_transaction((_pending(target, None, "changed\n"),), journal)

    (tmp_path / target).write_text("diverged\n", encoding="utf-8")
    with pytest.raises(ValueError, match="committed transaction"):
        publish_transaction((write,), journal)


def test_nonterminal_rerun_compensates_then_requires_new_transaction_id(tmp_path: Path) -> None:
    target = Path("catalog.md")
    target_path = tmp_path / target
    target_path.write_text("candidate\n", encoding="utf-8")
    write = _pending(target, "accepted\n", "candidate\n")
    journal = tmp_path / "transactions" / "run-1.md"
    journal.parent.mkdir(parents=True)
    replacing = PublicationResult(
        "run-1",
        "replacing",
        (replace(write, state="replaced"), _pending(Path("next.md"), None, "next\n")),
        Path("transactions/run-1.md"),
    )
    journal.write_text(render_publication_result(replacing), encoding="utf-8")

    with pytest.raises(ValueError, match="new transaction ID"):
        publish_transaction((write,), journal)

    assert target_path.read_text(encoding="utf-8") == "accepted\n"
    assert load_publication_result(journal).state == "compensated"


def test_nonterminal_rerun_detects_unjournaled_completed_replacement(tmp_path: Path) -> None:
    target = Path("catalog.md")
    target_path = tmp_path / target
    target_path.write_text("candidate\n", encoding="utf-8")
    write = _pending(target, "accepted\n", "candidate\n")
    journal = tmp_path / "transactions" / "run-1.md"
    journal.parent.mkdir(parents=True)
    prepared = PublicationResult(
        "run-1",
        "prepared",
        (write,),
        Path("transactions/run-1.md"),
    )
    journal.write_text(render_publication_result(prepared), encoding="utf-8")

    with pytest.raises(ValueError, match="new transaction ID"):
        publish_transaction((write,), journal)

    assert target_path.read_text(encoding="utf-8") == "accepted\n"
    assert load_publication_result(journal).state == "compensated"


def test_mid_transaction_cancellation_compensates_and_retains_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("catalog.md")
    journal = tmp_path / "transactions" / "cancelled.md"
    (tmp_path / target).write_text("before\n", encoding="utf-8")
    write = _pending(target, "before\n", "after\n")
    real_atomic_write = portfolio_research.atomic_write

    def cancel_after_replace(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == tmp_path / target and text == "after\n":
            raise KeyboardInterrupt

    monkeypatch.setattr(portfolio_research, "atomic_write", cancel_after_replace)

    result = publish_transaction((write,), journal)

    assert result.state == "compensated"
    assert (tmp_path / target).read_text(encoding="utf-8") == "before\n"
    assert load_publication_result(journal).state == "compensated"


def test_t1_outcomes_use_disposable_local_only_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="portfolio-research-u2-")).resolve()
    print(f"T1_FIXTURE_ROOT={root}")
    target = Path("catalog.md")
    target_path = root / target
    target_path.write_text("accepted\n", encoding="utf-8")
    write = _pending(target, "accepted\n", "candidate\n")
    real_atomic_write = portfolio_research.atomic_write

    success = publish_transaction((write,), root / "transactions" / "success.md")
    assert success.state == "committed"
    rerun = publish_transaction((write,), root / "transactions" / "success.md")
    assert rerun == success

    target_path.write_text("accepted\n", encoding="utf-8")

    def fail_and_refuse_compensation(path: Path, text: str) -> None:
        if path == target_path and text == "accepted\n":
            raise OSError("injected compensation failure")
        real_atomic_write(path, text)
        if path == target_path and text == "candidate\n":
            raise OSError("injected replacement boundary failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_and_refuse_compensation)
    manual = publish_transaction((write,), root / "transactions" / "manual.md")
    assert manual.state == "manual-recovery"
    with pytest.raises(ValueError, match="manual recovery"):
        ensure_readable(target, (manual,))

    monkeypatch.setattr(portfolio_research, "atomic_write", real_atomic_write)
    forced_target = Path("forced.md")
    forced_path = root / forced_target
    forced_path.write_text("accepted\n", encoding="utf-8")
    forced_write = _pending(forced_target, "accepted\n", "candidate\n")

    def fail_after_replacement(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == forced_path and text == "candidate\n":
            raise OSError("injected replacement boundary failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_after_replacement)
    compensated = publish_transaction((forced_write,), root / "transactions" / "forced.md")
    assert compensated.state == "compensated"
    assert forced_path.read_text(encoding="utf-8") == "accepted\n"

    cancel_target = Path("cancel.md")
    cancel_path = root / cancel_target
    cancel_path.write_text("accepted\n", encoding="utf-8")
    cancel_write = _pending(cancel_target, "accepted\n", "candidate\n")

    def cancel_after_replacement(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == cancel_path and text == "candidate\n":
            raise KeyboardInterrupt

    monkeypatch.setattr(portfolio_research, "atomic_write", cancel_after_replacement)
    cancelled = publish_transaction((cancel_write,), root / "transactions" / "cancelled.md")
    assert cancelled.state == "compensated"
    assert cancel_path.read_text(encoding="utf-8") == "accepted\n"

    planning_input = root / "planning-inputs.md"
    planning_input.write_text("approved\n", encoding="utf-8")
    headless_root = root / "headless"
    exit_status = portfolio_research_runtime.main(
        [
            "--root",
            str(headless_root),
            "--source-root",
            str(root),
            "--planning-input",
            str(planning_input),
            "--expected-digest",
            sha256(b"changed\n").hexdigest(),
            "discovery",
        ]
    )
    assert exit_status == 2
    assert not (headless_root / "transactions").exists()
    assert not (root / "transactions" / "pre-journal-cancelled.md").exists()


def test_stale_cleanup_requires_exact_three_set_allowlist_of_25(tmp_path: Path) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)

    writes = prepare_stale_disposition_cleanup(root, corrected_ids, superseded, inventory)

    assert len(writes) == 25
    assert {write.target for write in writes} == set(inventory)
    assert all(write.replacement_text == _canonical_empty_dispositions() for write in writes)


def test_stale_inventory_writer_reader_round_trip(tmp_path: Path) -> None:
    _root, _corrected_ids, _superseded, inventory = _stale_cleanup_fixture(tmp_path)
    path = tmp_path / "stale-inventory.md"
    rendered = render_stale_disposition_inventory(inventory)
    path.write_text(rendered, encoding="utf-8")

    loaded = load_stale_disposition_inventory(path)

    assert loaded == inventory
    assert render_stale_disposition_inventory(loaded) == rendered


def test_stale_cleanup_rejects_target_not_owned_by_superseded_journal(tmp_path: Path) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)
    target = next(iter(inventory))
    retained_writes = tuple(write for write in superseded.writes if write.target != target)
    superseded = PublicationResult(
        superseded.transaction_id,
        "committed",
        retained_writes,
        superseded.journal_path,
    )

    with pytest.raises(ValueError, match="exact stale allowlist"):
        prepare_stale_disposition_cleanup(root, corrected_ids, superseded, inventory)


@pytest.mark.parametrize("failure", ["outside", "symlink", "non-regular", "digest"])
def test_stale_cleanup_gates_fail_before_journal_creation(
    tmp_path: Path,
    failure: str,
) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)
    journal = root / "transactions" / "cleanup.md"
    target = next(iter(inventory))
    target_path = root / target
    if failure == "outside":
        outside = Path("dispositions/../catalog.md")
        inventory[outside] = inventory.pop(target)
    elif failure == "symlink":
        target_path.unlink()
        target_path.symlink_to(root / "catalog.md")
    elif failure == "non-regular":
        target_path.unlink()
        target_path.mkdir()
    else:
        target_path.write_text("diverged\n", encoding="utf-8")

    with pytest.raises(ValueError):
        prepare_stale_disposition_cleanup(root, corrected_ids, superseded, inventory)
    assert not journal.exists()


def test_cleanup_snapshots_then_journal_replaces_with_canonical_empty_documents(tmp_path: Path) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)
    snapshot_root = root / "runtime-evidence" / "U2" / "stale-snapshot"
    journal = root / "transactions" / "cleanup.md"

    result = cleanup_stale_dispositions(
        root,
        corrected_ids,
        superseded,
        inventory,
        snapshot_root,
        journal,
    )

    assert result.state == "committed"
    assert all((root / target).read_text(encoding="utf-8") == _canonical_empty_dispositions() for target in inventory)
    assert all((snapshot_root / target.name).is_file() for target in inventory)
    assert load_publication_result(journal).state == "committed"


@pytest.mark.parametrize("failure_after", range(1, 26))
def test_stale_cleanup_compensates_after_every_replacement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_after: int,
) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)
    before = {target: (root / target).read_bytes() for target in inventory}
    real_atomic_write = portfolio_research.atomic_write
    replacements = 0

    def fail_at_boundary(path: Path, text: str) -> None:
        nonlocal replacements
        real_atomic_write(path, text)
        if path.parent == root / "dispositions" and text == _canonical_empty_dispositions():
            replacements += 1
            if replacements == failure_after:
                raise OSError("injected stale cleanup boundary failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_at_boundary)

    result = cleanup_stale_dispositions(
        root,
        corrected_ids,
        superseded,
        inventory,
        root / "runtime-evidence" / "U2" / "snapshot",
        root / "transactions" / f"cleanup-{failure_after}.md",
    )

    assert result.state == "compensated"
    assert {target: (root / target).read_bytes() for target in inventory} == before


def test_headless_cleanup_only_writes_inventory_and_proposed_journal(tmp_path: Path) -> None:
    root, corrected_ids, superseded, inventory = _stale_cleanup_fixture(tmp_path)
    before = {target: (root / target).read_bytes() for target in inventory}
    inventory_path = root / "runtime-evidence" / "U2" / "stale-disposition-inventory.md"
    proposal = root / "runtime-evidence" / "U2" / "cleanup-proposal.md"

    prepared = prepare_stale_cleanup_headless(
        root,
        corrected_ids,
        superseded,
        inventory,
        inventory_path,
        proposal,
    )

    assert prepared.state == "prepared"
    assert inventory_path.read_text(encoding="utf-8") == render_stale_disposition_inventory(inventory)
    assert load_publication_result(proposal).state == "prepared"
    assert {target: (root / target).read_bytes() for target in inventory} == before
    assert not (root / "transactions" / "cleanup.md").exists()


def test_feature_scoped_evidence_preserves_historical_bytes(tmp_path: Path) -> None:
    historical = tmp_path / ".release-loop" / "evidence" / "U2"
    historical.mkdir(parents=True)
    historical_files = {
        historical / "t1-success.md": b"historical-success\n",
        historical / "t1-headless.md": b"historical-headless\n",
    }
    for path, content in historical_files.items():
        path.write_bytes(content)
    before = {path: path.read_bytes() for path in historical_files}
    records = {
        name: f"# {name}\n\nBOUNDARY_SENTINEL=local-only\n"
        for name in (
            "T1-success.md",
            "T1-forced-failure.md",
            "T1-rerun.md",
            "T1-compensation.md",
            "T1-headless.md",
            "T1-cancellation.md",
        )
    }

    evidence_root = write_sanitized_t1_evidence(tmp_path, records)

    assert evidence_root == tmp_path / ".release-loop" / "evidence" / "portfolio-U2"
    assert sorted(path.name for path in evidence_root.glob("T1-*.md")) == sorted(records)
    assert {path: path.read_bytes() for path in historical_files} == before


def test_build_index_is_deterministic_and_groups_evidence_fields() -> None:
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "parser failure",
        "Python parser", "implemented parser", "pytest", "stable output", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )

    first = build_index((), (evidence,))
    second = build_index((), (evidence,))

    assert first == second
    assert "context-1" in first and "period-1" in first and "project-1" in first
    assert "Python parser" in first and "parser failure" in first and "e1" in first


def test_build_index_escapes_multiline_cells() -> None:
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "line one\nline two",
        "Python parser", "implemented parser", "pytest", "stable output", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )

    rendered = build_index((), (evidence,))

    assert "line one<br>line two" in rendered
    assert rendered.count("| Context |") == 1
    assert rendered.count("| context-1 |") == 1


def test_build_index_includes_narrative_source_mapping() -> None:
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )

    rendered = build_index((), (evidence,), {"project-1": "companies/example/portfolios/parser.md"})

    assert "companies/example/portfolios/parser.md" in rendered


def test_load_narrative_sources_reads_project_mapping_table(tmp_path: Path) -> None:
    mapping = tmp_path / "project-mapping.md"
    mapping.write_text(
        "\n".join(
            (
                "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| FNS | project-1 | fassker/api | companies/FNS/portfolios/fassker-api.md | token match | confirmed | 3 |",
                "| FNS | project-2 | fassker/unknown |  | no match | unmapped | 1 |",
                "",
            )
        ),
        encoding="utf-8",
    )

    assert portfolio_research._load_narrative_sources(mapping) == {
        "project-1": "companies/FNS/portfolios/fassker-api.md",
    }


def test_prepare_proposal_rejects_unsafe_id_before_writing_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (evidence_root / "e1.md").write_text(render_evidence(evidence), encoding="utf-8")

    with pytest.raises(ValueError, match="safe identifier"):
        prepare_proposal(root, "../escape", Path("profile/portfolios/p.md"), "after", ("e1",))
    assert not (root / "snapshots").exists()


def test_apply_proposal_requires_approval_and_matching_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "companies" / "c1" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    snapshot = root / "snapshots" / "proposal-1" / "companies" / "c1" / "portfolios" / "p.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "parser failure",
        "Python parser", "implemented parser", "pytest", "stable output", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (evidence_root / "e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    source_path = Path("companies/c1/portfolios/p.md")
    _write_proposal_authority(root, source_path)
    digest = sha256(b"before\n").hexdigest()
    draft = NarrativeProposal("proposal-1", source_path, digest,
        Path("snapshots/proposal-1/companies/c1/portfolios/p.md"), digest,
        "before\n", "after [Evidence: e1]\n", ("e1",), "draft", None, None)

    with pytest.raises(ValueError, match="approved"):
        apply_proposal(root, draft)

    approved = replace(draft, state="approved", approved_by="user", approved_at=datetime(2026, 8, 27, tzinfo=UTC))
    with pytest.raises(ValueError, match="proposal record"):
        apply_proposal(root, approved)
    (root / "proposals").mkdir()
    (root / "proposals" / "proposal-1.md").write_text(render_proposal(approved), encoding="utf-8")
    applied = apply_proposal(root, approved)

    assert applied.state == "applied"
    assert source.read_text(encoding="utf-8") == "after [Evidence: e1]\n"
    assert apply_proposal(root, applied) == applied
    source.write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output diverged"):
        apply_proposal(root, applied)


def test_revision_proposal_uses_review_sidecar_and_preserves_applied_history(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    first = prepare_proposal(root, "proposal-1", source_path, "first [Evidence: e1]\n", ("e1",))
    first = replace(first, state="approved", approved_by="user", approved_at=datetime(2026, 9, 5, tzinfo=UTC))
    (root / "proposals/proposal-1.md").write_text(render_proposal(first), encoding="utf-8")
    first = apply_proposal(root, first)

    review_path = Path("reviews/proposal-2.md")
    review = root / review_path
    review.parent.mkdir()
    review.write_text(
        "<!-- portfolio-review-only -->\n\nProduct source: https://example.test/product\nEvidence: e1\n",
        encoding="utf-8",
    )
    second = prepare_proposal(
        root,
        "proposal-2",
        source_path,
        "clean revised narrative [Evidence: e1]\n",
        ("e1",),
        review_path=review_path,
        supersedes="proposal-1",
    )
    assert second.review_digest == sha256(review.read_bytes()).hexdigest()
    assert second.supersedes == "proposal-1"
    second = replace(second, state="approved", approved_by="user", approved_at=datetime(2026, 9, 5, tzinfo=UTC))
    (root / "proposals/proposal-2.md").write_text(render_proposal(second), encoding="utf-8")
    second = apply_proposal(root, second)

    assert source.read_text(encoding="utf-8") == "clean revised narrative [Evidence: e1]\n"
    assert load_proposal(root / "proposals/proposal-1.md").proposed_text == "first [Evidence: e1]\n"
    portfolio_research._validate_proposal_revisions(root, (first, second))
    review.write_text("<!-- portfolio-review-only -->\nEvidence: e1\nchanged\n", encoding="utf-8")
    with pytest.raises(ValueError, match="review digest diverged"):
        portfolio_research._validate_proposal_review(root, second)


def test_superseded_draft_allows_one_clean_replacement_proposal(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    first = prepare_proposal(root, "proposal-1", source_path, "first [Evidence: e1]\n", ("e1",))
    replaced = portfolio_research.supersede_draft(root, first)
    review_path = Path("reviews/proposal-2.md")
    review = root / review_path
    review.parent.mkdir()
    review.write_text("<!-- portfolio-review-only -->\nEvidence: e1\n", encoding="utf-8")

    second = prepare_proposal(
        root,
        "proposal-2",
        source_path,
        "clean replacement [Evidence: e1]\n",
        ("e1",),
        review_path=review_path,
    )

    assert load_proposal(root / "proposals/proposal-1.md").state == "superseded"
    portfolio_research._validate_proposal_revisions(root, (replaced, second))


def test_prepare_proposal_rerun_with_same_args_returns_stored_proposal(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    first = prepare_proposal(root, "proposal-1", source_path, "after [Evidence: e1]\n", ("e1",))
    second = prepare_proposal(root, "proposal-1", source_path, "after [Evidence: e1]\n", ("e1",))

    assert isinstance(second, NarrativeProposal)
    assert second == first == load_proposal(root / "proposals/proposal-1.md")


def test_prepare_proposal_supersedes_rerun_excludes_its_own_record(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    first = prepare_proposal(root, "proposal-1", source_path, "first [Evidence: e1]\n", ("e1",))
    first = replace(first, state="approved", approved_by="user", approved_at=datetime(2026, 9, 5, tzinfo=UTC))
    (root / "proposals/proposal-1.md").write_text(render_proposal(first), encoding="utf-8")
    apply_proposal(root, first)

    second = prepare_proposal(
        root, "proposal-2", source_path, "second [Evidence: e1]\n", ("e1",), supersedes="proposal-1",
    )
    again = prepare_proposal(
        root, "proposal-2", source_path, "second [Evidence: e1]\n", ("e1",), supersedes="proposal-1",
    )

    assert isinstance(again, NarrativeProposal)
    assert again == second


def test_prepare_proposal_review_file_requires_line_citations_outside_boundary(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    review_path = Path("reviews/proposal-1.md")
    review = root / review_path
    review.parent.mkdir()
    review.write_text("<!-- portfolio-review-only -->\nEvidence: e1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="concrete claim requires an evidence reference"):
        prepare_proposal(root, "proposal-1", source_path, "uncited narrative\n", ("e1",), review_path=review_path)


def test_prepare_proposal_review_boundary_section_is_the_enumerated_exemption(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/c1/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (root / "evidence").mkdir(parents=True)
    (root / "evidence/e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    review_path = Path("reviews/proposal-1.md")
    review = root / review_path
    review.parent.mkdir()
    review.write_text("<!-- portfolio-review-only -->\nEvidence: e1\n", encoding="utf-8")
    proposed = (
        "## Evidence-grounded summary\n"
        "## Review Boundary\n"
        "claim-to-evidence mapping for this draft lives in reviews/proposal-1.md\n"
    )

    proposal = prepare_proposal(root, "proposal-1", source_path, proposed, ("e1",), review_path=review_path)

    assert proposal.proposed_text == proposed


def test_prepare_proposal_rejects_non_narrative_source(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "manifest.md"
    source.parent.mkdir(parents=True)
    source.write_text("manifest\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (evidence_root / "e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, Path("companies/example/portfolios/p.md"))

    with pytest.raises(ValueError, match="narrative source"):
        prepare_proposal(root, "proposal-1", Path("manifest.md"), "after\n[Evidence: e1]\n", ("e1",))


def test_u7_validation_runs_privacy_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _lane_validation_fixture(tmp_path)
    (root / "proposals").mkdir()
    (root / "project-mapping.md").write_text("# Project mapping\n", encoding="utf-8")
    _occurrences, logical = load_catalog(root / "catalog.md")
    evidence = [
        load_evidence(path)
        for path in sorted((root / "evidence").glob("*.md"))
    ]
    (root / "index.md").write_text(build_index(logical, evidence), encoding="utf-8")

    monkeypatch.setattr(
        portfolio_research,
        "_validate_evidence_privacy",
        lambda _evidence: (_ for _ in ()).throw(ValueError("privacy: injected")),
    )

    issues = validate_research(
        root,
        ("proposals", "references", "contexts", "evidence-shape", "privacy"),
    )

    assert issues and issues[0].message == "privacy: injected"


def test_u7_validation_rejects_symlinked_evidence_root(tmp_path: Path) -> None:
    root = _lane_validation_fixture(tmp_path)
    (root / "proposals").mkdir()
    (root / "project-mapping.md").write_text("# Project mapping\n", encoding="utf-8")
    _occurrences, logical = load_catalog(root / "catalog.md")
    evidence = [load_evidence(path) for path in sorted((root / "evidence").glob("*.md"))]
    (root / "index.md").write_text(build_index(logical, evidence), encoding="utf-8")
    evidence_path = root / "evidence"
    evidence_path.rename(root / "evidence-real")
    evidence_path.symlink_to(root / "evidence-real", target_is_directory=True)

    issues = validate_research(
        root,
        ("proposals", "references", "contexts", "evidence-shape", "privacy"),
    )

    assert issues and "evidence" in issues[0].message


def test_u7_validation_rejects_uncited_proposal_claim(tmp_path: Path) -> None:
    root = _lane_validation_fixture(tmp_path)
    (root / "project-mapping.md").write_text(
        "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| context-1 | project-1 | fixture | companies/example/portfolios/p.md | fixture | confirmed | e1 |\n",
        encoding="utf-8",
    )
    source = root.parent / "companies/example/portfolios/p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    proposal = replace(
        _minimal_proposal("proposal-1"),
        source_path=Path("companies/example/portfolios/p.md"),
        snapshot_path=Path("snapshots/proposal-1/companies/example/portfolios/p.md"),
        proposed_text="uncited claim\n[Evidence: evidence-1]\n",
        evidence_ids=("evidence-1",),
    )
    snapshot = root / proposal.snapshot_path
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("before\n", encoding="utf-8")
    _occurrences, logical = load_catalog(root / "catalog.md")
    evidence = [load_evidence(path) for path in sorted((root / "evidence").glob("*.md"))]
    mapping = portfolio_research._load_narrative_sources(root / "project-mapping.md", root=root)
    (root / "index.md").write_text(build_index(logical, evidence, mapping), encoding="utf-8")
    (root / "proposals").mkdir()
    (root / "proposals/proposal-1.md").write_text(render_proposal(proposal), encoding="utf-8")
    issues = validate_research(root, ("proposals", "references", "contexts"))

    assert issues and issues[0].code == "invalid-artifact"
    assert "concrete claim" in issues[0].message


def test_prepare_proposal_rejects_symlinked_research_parent(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
        "technology", "contribution", "verification", "outcome", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (evidence_root / "e1.md").write_text(render_evidence(evidence), encoding="utf-8")
    _write_proposal_authority(root, Path("profile/portfolios/p.md"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "snapshots").mkdir(parents=True)
    (root / "snapshots" / "proposal-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="research path parent"):
        prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after [Evidence: e1]\n", ("e1",))


def test_prepare_proposal_rejects_symlinked_narrative_file(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    real_source = root.parent / "companies" / "c1" / "portfolios" / "p.md"
    alias_source = root.parent / "profile" / "portfolios" / "alias.md"
    real_source.parent.mkdir(parents=True)
    alias_source.parent.mkdir(parents=True)
    real_source.write_text("before\n", encoding="utf-8")
    alias_source.symlink_to(real_source)
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "e1.md").write_text(render_evidence(_lane_evidence()), encoding="utf-8")
    _write_proposal_authority(root, Path("profile/portfolios/alias.md"))

    with pytest.raises(ValueError, match="must not be a symlink"):
        prepare_proposal(root, "proposal-1", Path("profile/portfolios/alias.md"), "after\n[Evidence: evidence-1]\n", ("evidence-1",))


def test_prepare_proposal_rejects_symlinked_evidence_file(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    outside = tmp_path / "outside-evidence.md"
    outside.write_text(render_evidence(_lane_evidence()), encoding="utf-8")
    (evidence_root / "evidence-1.md").symlink_to(outside)
    _write_proposal_authority(root, Path("profile/portfolios/p.md"))

    with pytest.raises(ValueError, match="evidence files must be regular"):
        prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after\n[Evidence: evidence-1]\n", ("evidence-1",))


def test_publish_transaction_rejects_symlinked_journal(tmp_path: Path) -> None:
    root = tmp_path / "portfolio-research"
    (root / "transactions").mkdir(parents=True)
    outside = tmp_path / "outside-journal.md"
    outside.write_text("do not trust\n", encoding="utf-8")
    journal = root / "transactions" / "proposal.md"
    journal.symlink_to(outside)

    with pytest.raises(ValueError, match="journal must be a regular file"):
        publish_transaction((_pending(Path("target.md"), None, "after\n"),), journal)


def test_prepare_proposal_rejects_cross_context_evidence(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "companies" / "example" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    evidence = replace(_lane_evidence(), context_id="other-context")
    (root / "evidence" / "evidence-1.md").write_text(render_evidence(evidence), encoding="utf-8")
    (root / "manifest.md").write_text(render_manifest(_manifest()), encoding="utf-8")
    _write_proposal_authority(root, Path("companies/example/portfolios/p.md"))

    with pytest.raises(ValueError, match="context does not match"):
        prepare_proposal(root, "proposal-1", Path("companies/example/portfolios/p.md"), "after\n[Evidence: evidence-1]\n", ("evidence-1",))


def test_prepare_proposal_rejects_unmapped_project_when_mapping_exists(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    (root / "project-mapping.md").write_text(
        "\n".join(
            (
                "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| FNS | project-1 | unknown |  | no match | unmapped | 1 |",
                "",
            )
        ),
        encoding="utf-8",
    )
    _write_proposal_authority(root, Path("profile/portfolios/p.md"))
    (root / "project-mapping.md").write_text(
        "\n".join(
            (
                "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| FNS | project-1 | unknown |  | no match | unmapped | 1 |",
                "",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not mapped"):
        prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after\n[Evidence: evidence-1]\n", ("evidence-1",))


def test_load_narrative_sources_rejects_invalid_confirmed_path(tmp_path: Path) -> None:
    mapping = tmp_path / "project-mapping.md"
    mapping.write_text(
        "\n".join(
            (
                "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| FNS | project-1 | fassker/api | ../outside.md | invalid | confirmed | 1 |",
                "",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="narrative source must be relative"):
        portfolio_research._load_narrative_sources(mapping)


@pytest.mark.parametrize(
    ("failure", "diverge_before_compensation"),
    ((OSError, False), (KeyboardInterrupt, False), (OSError, True)),
)
def test_apply_proposals_compensates_partial_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
    diverge_before_compensation: bool,
) -> None:
    apply_proposals = getattr(portfolio_research, "apply_proposals", None)
    assert apply_proposals is not None
    root = tmp_path / "private" / "portfolio-research"
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    proposals_root = root / "proposals"
    proposals_root.mkdir()
    _write_proposal_authority(root, Path("profile/portfolios/p1.md"), project_ids=("project-1", "project-2"))
    (root / "project-mapping.md").write_text(
        "\n".join(
            (
                "| Context | Project ID | Repository token | Narrative source | Rationale | State | Evidence ledgers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                "| context-1 | project-1 | fixture | profile/portfolios/p1.md | fixture | confirmed | e1 |",
                "| context-1 | project-2 | fixture | profile/portfolios/p2.md | fixture | confirmed | e2 |",
                "",
            )
        ),
        encoding="utf-8",
    )
    sources: list[Path] = []
    proposals: list[NarrativeProposal] = []
    for index in (1, 2):
        source = root.parent / "profile" / "portfolios" / f"p{index}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"before-{index}\n", encoding="utf-8")
        snapshot_path = root / "snapshots" / f"proposal-{index}" / "profile" / "portfolios" / f"p{index}.md"
        snapshot_path.parent.mkdir(parents=True)
        snapshot_path.write_text(f"before-{index}\n", encoding="utf-8")
        evidence = EvidenceRecord(
            f"e{index}", "context-1", f"project-{index}", f"work-{index}", ("period-1",),
            ("logical-1",), ("a" * 40,), "yes", "yes", "problem",
            "technology", "contribution", "verification", "outcome", (),
            (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
            "main=" + "a" * 40, "d" * 64, "complete", "none",
        )
        (evidence_root / f"e{index}.md").write_text(render_evidence(evidence), encoding="utf-8")
        digest = sha256(f"before-{index}\n".encode()).hexdigest()
        proposal = NarrativeProposal(
            f"proposal-{index}",
            Path("profile") / "portfolios" / f"p{index}.md",
            digest,
            Path("snapshots") / f"proposal-{index}" / "profile" / "portfolios" / f"p{index}.md",
            digest,
            f"before-{index}\n",
                f"after-{index} [Evidence: e{index}]\n",
            (f"e{index}",),
            "approved",
            "user",
            datetime(2026, 8, 27, tzinfo=UTC),
        )
        (proposals_root / f"proposal-{index}.md").write_text(render_proposal(proposal), encoding="utf-8")
        sources.append(source)
        proposals.append(proposal)

    real_atomic_write = portfolio_research.atomic_write

    def fail_on_second_source(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == sources[1] and text == "after-2 [Evidence: e2]\n":
            if diverge_before_compensation:
                sources[0].write_text("diverged\n", encoding="utf-8")
            raise failure("injected batch failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_on_second_source)

    expected_state = "manual-recovery" if diverge_before_compensation else "compensated"
    with pytest.raises(ValueError, match=expected_state):
        apply_proposals(root, tuple(proposals))
    expected_sources = ["diverged\n", "before-2\n"] if diverge_before_compensation else ["before-1\n", "before-2\n"]
    assert [path.read_text(encoding="utf-8") for path in sources] == expected_sources
    assert [load_proposal(proposals_root / f"proposal-{index}.md").state for index in (1, 2)] == ["approved", "approved"]
    if diverge_before_compensation:
        journal = root / "transactions" / "proposal-proposal-1-proposal-2.md"
        assert load_publication_result(journal).state == "manual-recovery"
        return

    monkeypatch.setattr(portfolio_research, "atomic_write", real_atomic_write)
    applied = apply_proposals(root, tuple(proposals))

    assert [item.state for item in applied] == ["applied", "applied"]
    assert [path.read_text(encoding="utf-8") for path in sources] == ["after-1 [Evidence: e1]\n", "after-2 [Evidence: e2]\n"]
    assert (root / "transactions" / "proposal-proposal-1-proposal-2-retry-1.md").is_file()
    assert apply_proposals(root, tuple(applied)) == applied


def test_apply_proposals_rejects_duplicate_ids_and_sources(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    with pytest.raises(ValueError, match="duplicate proposal IDs"):
        portfolio_research.apply_proposals(root, (_minimal_proposal("same"), _minimal_proposal("same")))


def test_prepare_proposal_requires_authority_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/example/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="authority"):
        prepare_proposal(root, "proposal-1", source_path, "after [Evidence: evidence-1]\n", ("evidence-1",))


@pytest.mark.parametrize("authority_name", ["manifest.md", "project-mapping.md"])
def test_prepare_proposal_rejects_symlinked_authority_artifact(
    tmp_path: Path,
    authority_name: str,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/example/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    _write_proposal_authority(root, source_path)
    outside = tmp_path / f"outside-{authority_name}"
    authority = root / authority_name
    outside.write_text(authority.read_text(encoding="utf-8"), encoding="utf-8")
    authority.unlink()
    authority.symlink_to(outside)

    with pytest.raises(ValueError, match="authority"):
        prepare_proposal(root, "proposal-1", source_path, "after [Evidence: evidence-1]\n", ("evidence-1",))


def test_next_proposal_journal_reconciles_existing_retry_before_allocating_new_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    transactions = root / "transactions"
    transactions.mkdir(parents=True)
    transaction_id = "proposal-batch"
    base_write = PendingWrite(
        Path("profile/portfolios/p.md"),
        "before\n",
        "after\n",
        sha256(b"before\n").hexdigest(),
        sha256(b"after\n").hexdigest(),
        "compensated",
    )
    base_path = transactions / f"{transaction_id}.md"
    base_path.write_text(
        render_publication_result(
            PublicationResult(
                transaction_id,
                "compensated",
                (base_write,),
                Path(root.name) / "transactions" / f"{transaction_id}.md",
            )
        ),
        encoding="utf-8",
    )
    target = root.parent / "profile" / "portfolios" / "p.md"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    write = _pending(target.relative_to(root.parent), "before\n", "after\n")
    retry_path = transactions / f"{transaction_id}-retry-1.md"
    retry_path.write_text(
        render_publication_result(
            PublicationResult(
                f"{transaction_id}-retry-1",
                "prepared",
                (write,),
                Path(root.name) / "transactions" / retry_path.name,
            )
        ),
        encoding="utf-8",
    )

    next_path = portfolio_research._next_proposal_journal(root, transaction_id, (write,))

    assert next_path == transactions / f"{transaction_id}-retry-2.md"
    assert load_publication_result(retry_path).state == "compensated"


def test_next_proposal_journal_rejects_foreign_nonterminal_journal_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    transactions = root / "transactions"
    transactions.mkdir(parents=True)
    transaction_id = "proposal-batch"
    expected = _pending(Path("profile/portfolios/p.md"), "before\n", "after\n")
    foreign_target = root.parent / "profile" / "contact.md"
    foreign_target.parent.mkdir(parents=True)
    foreign_target.write_text("before-contact\n", encoding="utf-8")
    foreign = replace(
        _pending(Path("profile/contact.md"), "before-contact\n", "after-contact\n"),
        state="replaced",
    )
    journal = transactions / f"{transaction_id}.md"
    journal.write_text(
        render_publication_result(
            PublicationResult(
                transaction_id,
                "replacing",
                (foreign,),
                Path(root.name) / "transactions" / journal.name,
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match requested proposal batch"):
        portfolio_research._next_proposal_journal(root, transaction_id, (expected,))

    assert foreign_target.read_text(encoding="utf-8") == "before-contact\n"
    assert load_publication_result(journal).state == "replacing"


def test_next_proposal_journal_rejects_retry_ordinal_gap_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    transactions = root / "transactions"
    transactions.mkdir(parents=True)
    transaction_id = "proposal-batch"
    target = root.parent / "profile" / "portfolios" / "p.md"
    target.parent.mkdir(parents=True)
    target.write_text("after\n", encoding="utf-8")
    write = replace(
        _pending(Path("profile/portfolios/p.md"), "before\n", "after\n"),
        state="replaced",
    )
    gap = transactions / f"{transaction_id}-retry-2.md"
    gap.write_text(
        render_publication_result(
            PublicationResult(
                f"{transaction_id}-retry-2",
                "replacing",
                (write,),
                Path(root.name) / "transactions" / gap.name,
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retry journal sequence has a gap"):
        portfolio_research._next_proposal_journal(root, transaction_id, (write,))

    assert target.read_text(encoding="utf-8") == "after\n"
    assert load_publication_result(gap).state == "replacing"


def test_nonterminal_reconciliation_persists_manual_recovery_on_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    target = Path("catalog.md")
    target_path = root / target
    target_path.write_text("after\n", encoding="utf-8")
    journal = root / "transactions" / "reconcile.md"
    write = PendingWrite(
        target,
        "before\n",
        "after\n",
        sha256(b"before\n").hexdigest(),
        sha256(b"after\n").hexdigest(),
        "replaced",
    )
    existing = PublicationResult("reconcile", "replacing", (write,), Path("transactions/reconcile.md"))
    real_atomic_write = portfolio_research.atomic_write

    def fail_restore(path: Path, text: str) -> None:
        if path == target_path and text == "before\n":
            raise ValueError("injected validation failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_restore)

    with pytest.raises(ValueError, match="manual recovery"):
        portfolio_research._reconcile_nonterminal_transaction(existing, root, journal)
    result = load_publication_result(journal)
    assert result.state == "manual-recovery"
    assert result.writes[0].state == "manual-recovery"


def test_prepare_proposal_rejects_claim_without_inline_evidence_reference(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/example/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    _write_proposal_authority(root, source_path)

    with pytest.raises(ValueError, match="evidence reference"):
        prepare_proposal(root, "proposal-1", source_path, "unsupported claim\n", ("evidence-1",))


def test_prepare_proposal_allows_unchanged_source_title_without_evidence_reference(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/example/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("# Existing portfolio title\n\nbefore\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    _write_proposal_authority(root, source_path)

    proposal = prepare_proposal(
        root,
        "proposal-1",
        source_path,
        "# Existing portfolio title\n\n## Evidence-grounded summary\n\n### Problem observed\n\nSupported claim [Evidence: evidence-1]\n",
        ("evidence-1",),
    )

    assert proposal.state == "draft"


@pytest.mark.parametrize(
    ("proposed_text", "message"),
    (
        ("Migrated production to Kubernetes.\n\nReferences: [Evidence: evidence-1]\n", "evidence reference"),
        ("## Evidence-grounded summary\n- Supported claim [Evidence: evidence-1]\n- Unsupported claim\n", "evidence reference"),
        ("## Evidence-grounded summary\n- Unsupported claim [Evidence: evidence-1\n", "evidence reference"),
        ("## Review boundary\nMigrated production to Kubernetes.\n\n[Evidence: evidence-1]\n", "evidence reference"),
        ("## Migrated production to Kubernetes\nReferences [Evidence: evidence-1]\n", "evidence reference"),
        ("Migrated production to Kubernetes. [Evidence: evidence-1] trailing claim\n", "evidence reference"),
        ("Migrated production to Kubernetes. <!-- [Evidence: evidence-1] -->\n", "HTML comments"),
    ),
)
def test_prepare_proposal_rejects_each_uncited_or_malformed_concrete_claim(
    tmp_path: Path,
    proposed_text: str,
    message: str,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("companies/example/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    _write_proposal_authority(root, source_path)

    with pytest.raises(ValueError, match=message):
        prepare_proposal(root, "proposal-1", source_path, proposed_text, ("evidence-1",))


def test_prepare_proposal_cancellation_before_snapshot_leaves_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("profile/portfolios/p.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(
        render_evidence(_lane_evidence()), encoding="utf-8"
    )
    _write_proposal_authority(root, source_path)
    real_atomic_write = portfolio_research.atomic_write

    def cancel_snapshot(path: Path, text: str) -> None:
        if path == root / "snapshots" / "proposal-1" / source_path:
            raise KeyboardInterrupt("injected snapshot cancellation")
        real_atomic_write(path, text)

    monkeypatch.setattr(portfolio_research, "atomic_write", cancel_snapshot)

    with pytest.raises(KeyboardInterrupt, match="snapshot cancellation"):
        prepare_proposal(root, "proposal-1", source_path, "after [Evidence: evidence-1]\n", ("evidence-1",))
    assert not (root / "snapshots").exists()
    assert not (root / "proposals").exists()
    assert source.read_text(encoding="utf-8") == "before\n"


def test_prepare_proposal_compensates_partial_record_when_source_matches_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("profile/portfolios/source.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(render_evidence(_lane_evidence()), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    snapshot = root / "snapshots" / "proposal-1" / source_path
    proposal = root / "proposals" / "proposal-1.md"
    real_atomic_write = portfolio_research.atomic_write

    def fail_after_proposal_record(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == proposal:
            raise OSError("injected proposal publication failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_after_proposal_record)

    with pytest.raises(OSError, match="proposal publication failure"):
        prepare_proposal(root, "proposal-1", source_path, "after [Evidence: evidence-1]\n", ("evidence-1",))

    assert not snapshot.exists()
    assert not proposal.exists()
    assert source.read_text(encoding="utf-8") == "before\n"


def test_prepare_proposal_preserves_partial_record_when_source_diverges_before_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source_path = Path("profile/portfolios/source.md")
    source = root.parent / source_path
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "evidence-1.md").write_text(render_evidence(_lane_evidence()), encoding="utf-8")
    _write_proposal_authority(root, source_path)
    snapshot = root / "snapshots" / "proposal-1" / source_path
    proposal = root / "proposals" / "proposal-1.md"
    real_atomic_write = portfolio_research.atomic_write

    def diverge_then_fail_after_proposal_record(path: Path, text: str) -> None:
        real_atomic_write(path, text)
        if path == proposal:
            source.write_text("diverged\n", encoding="utf-8")
            raise OSError("injected proposal publication failure")

    monkeypatch.setattr(portfolio_research, "atomic_write", diverge_then_fail_after_proposal_record)

    with pytest.raises(OSError, match="proposal publication failure"):
        prepare_proposal(root, "proposal-1", source_path, "after [Evidence: evidence-1]\n", ("evidence-1",))

    assert snapshot.read_text(encoding="utf-8") == "before\n"
    assert proposal.is_file()
    assert source.read_text(encoding="utf-8") == "diverged\n"


def _minimal_proposal(proposal_id: str) -> NarrativeProposal:
    digest = sha256(b"before\n").hexdigest()
    return NarrativeProposal(
        proposal_id,
        Path("profile/portfolios/p.md"),
        digest,
        Path("snapshots") / proposal_id / "profile/portfolios/p.md",
        digest,
        "before\n",
        "after\n",
        ("e1",),
        "approved",
        "user",
        datetime(2026, 8, 27, tzinfo=UTC),
    )


def test_prepare_proposal_snapshots_source_and_rejects_unknown_evidence(tmp_path: Path) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    _write_proposal_authority(root, Path("profile/portfolios/p.md"))
    evidence = EvidenceRecord(
        "e1", "context-1", "project-1", "work-1", ("period-1",),
        ("logical-1",), ("a" * 40,), "yes", "yes", "parser failure",
        "Python parser", "implemented parser", "pytest", "stable output", (),
        (EvidenceLink("logical-1", "a" * 40, Path("src/parser.py"), "parse", "contribution"),),
        "main=" + "a" * 40, "d" * 64, "complete", "none",
    )
    (evidence_root / "e1.md").write_text(render_evidence(evidence), encoding="utf-8")

    proposal = prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after [Evidence: e1]\n", ("e1",))

    assert proposal.state == "draft"
    assert (root / proposal.snapshot_path).read_text(encoding="utf-8") == "before\n"
    assert load_proposal(root / "proposals/proposal-1.md") == proposal
    with pytest.raises(ValueError, match="unknown evidence"):
        prepare_proposal(root, "proposal-2", Path("profile/portfolios/p.md"), "after [Evidence: missing]\n", ("missing",))


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_prepare_proposal_retries_after_proposal_record_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    root = tmp_path / "private" / "portfolio-research"
    source = root.parent / "profile" / "portfolios" / "p.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\n", encoding="utf-8")
    evidence_root = root / "evidence"
    evidence_root.mkdir(parents=True)
    (evidence_root / "e1.md").write_text(render_evidence(_lane_evidence()), encoding="utf-8")
    _write_proposal_authority(root, Path("profile/portfolios/p.md"))
    real_atomic_write = portfolio_research.atomic_write
    proposal_path = root / "proposals" / "proposal-1.md"

    def fail_proposal_record(path: Path, text: str) -> None:
        if path == proposal_path:
            raise failure("injected proposal record failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(portfolio_research, "atomic_write", fail_proposal_record)

    with pytest.raises(failure, match="proposal record"):
        prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after [Evidence: evidence-1]\n", ("evidence-1",))
    assert (root / "snapshots" / "proposal-1" / "profile" / "portfolios" / "p.md").read_text(encoding="utf-8") == "before\n"
    assert not proposal_path.exists()

    monkeypatch.setattr(portfolio_research, "atomic_write", real_atomic_write)
    proposal = prepare_proposal(root, "proposal-1", Path("profile/portfolios/p.md"), "after [Evidence: evidence-1]\n", ("evidence-1",))

    assert proposal.state == "draft"
    assert load_proposal(proposal_path) == proposal


def _stale_cleanup_fixture(
    tmp_path: Path,
) -> tuple[Path, set[str], PublicationResult, dict[Path, str]]:
    root = tmp_path / "portfolio-research"
    dispositions = root / "dispositions"
    dispositions.mkdir(parents=True)
    (root / "catalog.md").write_text("catalog\n", encoding="utf-8")
    corrected_ids = {"logical-current"}
    inventory: dict[Path, str] = {}
    journal_writes = []
    for index in range(25):
        target = Path("dispositions") / f"logical-stale-{index:02d}.md"
        text = f"stale-{index}\n"
        (root / target).write_text(text, encoding="utf-8")
        digest = sha256(text.encode()).hexdigest()
        inventory[target] = digest
        journal_writes.append(
            PendingWrite(target, None, text, None, digest, "replaced")
        )
    superseded = PublicationResult(
        "u2-initial",
        "committed",
        tuple(journal_writes),
        Path("transactions/u2-initial.md"),
    )
    return root, corrected_ids, superseded, inventory


def _canonical_empty_dispositions() -> str:
    from careerkit.resume.adapters.portfolio_markdown import render_dispositions

    return render_dispositions(())


def _disposition(commit_id: str) -> CommitDisposition:
    return CommitDisposition(
        "logical-1",
        commit_id,
        "author-1",
        datetime(2020, 1, 1, tzinfo=UTC),
        True,
        "no",
        "no",
        "no",
        "no",
        "unassigned",
        (),
        None,
        "contribution",
    )


def _pending(target: Path, original: str | None, replacement: str) -> PendingWrite:
    return PendingWrite(
        target,
        original,
        replacement,
        None if original is None else sha256(original.encode()).hexdigest(),
        sha256(replacement.encode()).hexdigest(),
    )
