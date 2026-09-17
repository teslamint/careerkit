from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from careerkit.resume.adapters import portfolio_markdown
from careerkit.resume.adapters.portfolio_markdown import (
    atomic_write,
    ensure_readable,
    load_catalog,
    load_dispositions,
    load_evidence,
    load_manifest,
    load_proposal,
    load_publication_result,
    render_catalog,
    render_dispositions,
    render_evidence,
    render_manifest,
    render_proposal,
    render_publication_result,
)
from careerkit.resume.domain.portfolio_evidence import (
    AuthorIdentity,
    CommitDisposition,
    ContextMapping,
    EmploymentPeriod,
    EvidenceLink,
    EvidenceRecord,
    LogicalRepository,
    NarrativeProposal,
    PendingWrite,
    PublicationResult,
    RawRepositoryOccurrence,
    ResearchManifest,
    validate_period_references,
)


UTC = timezone.utc


@pytest.fixture
def complete_records() -> tuple[ResearchManifest, list[RawRepositoryOccurrence], list[LogicalRepository], list[CommitDisposition], EvidenceRecord, NarrativeProposal]:
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (ContextMapping("c1", "company", (Path("/approved/company"),), Path("companies/c1"), ("p1",)),),
        (AuthorIdentity("a1", "Example Author", "author@example.test"),),
        (EmploymentPeriod("p1", "c1", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),),
    )
    occurrences = [RawRepositoryOccurrence("o1", Path("repo"), "standard", Path("git"), "objects", ("refs/heads/main=" + "a" * 40,), "r1", None, "resolved")]
    logical = [LogicalRepository("r1", ("o1",), "c1", ("p1",), "yes", "a" * 40, "b" * 40, 2, "cataloged", "pending", "high signal", None, ("refs/heads/main=" + "a" * 40,), "d" * 64)]
    dispositions = [CommitDisposition("r1", "a" * 40, "a1", datetime(2020, 6, 1, tzinfo=UTC), True, "no", "no", "no", "no", "work-unit", ("w1",), None, "contribution")]
    evidence = EvidenceRecord("e1", "c1", "project-1", "work-1", ("p1",), ("r1",), ("a" * 40, "b" * 40), "yes", "yes", "problem", "Python | service | implemented | commit", "implemented parser", "pytest", "stable output", ("none",), (EvidenceLink("r1", "a" * 40, Path("src/a.py"), "parse", "contribution"), EvidenceLink("r1", "b" * 40, Path("tests/a.py"), "test_parse", "context")), "main=" + "a" * 40, "d" * 64, "complete", "none")
    proposal = NarrativeProposal("n1", Path("companies/c1/portfolios/p.md"), sha256(b"old").hexdigest(), Path("snapshots/n1/p.md"), sha256(b"old").hexdigest(), "old", "new [e1]", ("e1",), "approved", "reviewer", datetime(2024, 1, 1, tzinfo=UTC))
    return manifest, occurrences, logical, dispositions, evidence, proposal


@pytest.mark.parametrize(
    ("index", "renderer", "loader"),
    [
        (0, lambda values: render_manifest(values[0]), load_manifest),
        (1, lambda values: render_catalog(values[1], values[2]), load_catalog),
        (3, lambda values: render_dispositions(values[3]), load_dispositions),
        (4, lambda values: render_evidence(values[4]), load_evidence),
        (5, lambda values: render_proposal(values[5]), load_proposal),
    ],
)
def test_writer_reader_pairs_are_canonical_and_byte_stable(tmp_path: Path, complete_records: tuple[object, ...], index: int, renderer: object, loader: object) -> None:
    first = renderer(complete_records)  # type: ignore[operator]
    path = tmp_path / "record.md"
    path.write_text(first, encoding="utf-8")
    parsed = loader(path)  # type: ignore[operator]
    if index == 1:
        second = render_catalog(*parsed)
    else:
        second = renderer((*complete_records[:index], parsed, *complete_records[index + 1 :]))  # type: ignore[operator]
    path.write_text(second, encoding="utf-8")
    reparsed = loader(path)  # type: ignore[operator]
    if index == 1:
        third = render_catalog(*reparsed)
    else:
        third = renderer((*complete_records[:index], reparsed, *complete_records[index + 1 :]))  # type: ignore[operator]
    assert first == second == third
    assert sha256(first.encode()).digest() == sha256(third.encode()).digest()


def test_personal_manifest_round_trips_three_roots_and_multiple_periods(tmp_path: Path) -> None:
    periods = (
        EmploymentPeriod("p1", "Employment A", datetime(2020, 1, 1, tzinfo=UTC), datetime(2021, 1, 1, tzinfo=UTC)),
        EmploymentPeriod("p2", "Employment B", datetime(2021, 1, 2, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC)),
    )
    manifest = ResearchManifest(
        "portfolio-manifest/v1",
        (
            ContextMapping(
                "personal",
                "personal-open-source",
                (Path("/approved/one"), Path("/approved/two"), Path("/approved/three")),
                Path("portfolios/personal"),
                ("p1", "p2"),
            ),
        ),
        (AuthorIdentity("a1", "Example Author", "author@example.test"),),
        periods,
    )
    text = render_manifest(manifest)
    path = _write_fixture(text, tmp_path / "manifest.md")

    loaded = load_manifest(path)

    assert loaded == manifest
    assert loaded.periods[0].employment_label == "Employment A"
    assert render_manifest(loaded) == text


def test_catalog_and_continuous_evidence_round_trip_multiple_period_ids(
    tmp_path: Path,
    complete_records: tuple[object, ...],
) -> None:
    occurrences = complete_records[1]
    logical = complete_records[2]
    evidence = complete_records[4]
    assert isinstance(occurrences, list)
    assert isinstance(logical, list)
    assert isinstance(evidence, EvidenceRecord)
    changed_logical = [replace(logical[0], period_ids=("p1", "p2"))]
    changed_evidence = replace(evidence, period_ids=("p1", "p2"))
    catalog_text = render_catalog(occurrences, changed_logical)
    evidence_text = render_evidence(changed_evidence)

    loaded_catalog = load_catalog(_write_fixture(catalog_text, tmp_path / "catalog.md"))
    loaded_evidence = load_evidence(_write_fixture(evidence_text, tmp_path / "evidence.md"))

    assert loaded_catalog == (occurrences, changed_logical)
    assert loaded_evidence == changed_evidence
    assert render_catalog(*loaded_catalog) == catalog_text
    assert render_evidence(loaded_evidence) == evidence_text


def test_separate_boundary_evidence_records_remain_distinct(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    evidence = complete_records[4]
    assert isinstance(evidence, EvidenceRecord)
    before = replace(evidence, evidence_id="e-before", work_unit_id="work-before", period_ids=("p1",))
    after = replace(evidence, evidence_id="e-after", work_unit_id="work-after", period_ids=("p2",))

    loaded_before = load_evidence(_write_fixture(render_evidence(before), tmp_path / "before.md"))
    loaded_after = load_evidence(_write_fixture(render_evidence(after), tmp_path / "after.md"))

    assert loaded_before != loaded_after
    assert loaded_before.period_ids == ("p1",)
    assert loaded_after.period_ids == ("p2",)


@pytest.mark.parametrize("mutation", ["schema: portfolio-unknown/v1", "unknown_field: true"])
def test_unknown_schema_and_frontmatter_field_fail_closed(tmp_path: Path, complete_records: tuple[object, ...], mutation: str) -> None:
    text = render_manifest(complete_records[0])  # type: ignore[arg-type]
    if mutation.startswith("schema"):
        text = text.replace("schema: portfolio-manifest/v1", mutation, 1)
    else:
        text = text.replace("---\n", f"---\n{mutation}\n", 1)
    path = tmp_path / "manifest.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)


def test_extra_fixed_table_column_fails_closed(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    text = render_evidence(complete_records[4])  # type: ignore[arg-type]
    text = text.replace("| Evidence role |", "| Evidence role | Extra |", 1)
    path = tmp_path / "evidence.md"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        load_evidence(path)


def test_duplicate_catalog_ids_fail_closed(complete_records: tuple[object, ...]) -> None:
    occurrences = complete_records[1]
    logical = complete_records[2]
    assert isinstance(occurrences, list)
    assert isinstance(logical, list)
    with pytest.raises(ValueError, match="duplicate occurrence_id"):
        render_catalog([*occurrences, occurrences[0]], logical)


def test_changed_evidence_role_changes_named_field_and_digest(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    evidence = complete_records[4]
    assert isinstance(evidence, EvidenceRecord)
    changed_link = replace(evidence.links[0], role="context")
    changed = replace(evidence, links=(changed_link, *evidence.links[1:]))
    original_text = render_evidence(evidence)
    changed_text = render_evidence(changed)

    assert "| contribution |" in original_text
    assert "| context |" in changed_text
    assert sha256(original_text.encode()).digest() != sha256(changed_text.encode()).digest()


def test_integration_records_resolve_all_cross_references(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    manifest, occurrences, logical, dispositions, evidence, proposal = complete_records
    paths = {name: tmp_path / f"{name}.md" for name in ("manifest", "catalog", "dispositions", "evidence", "proposal")}
    paths["manifest"].write_text(render_manifest(manifest), encoding="utf-8")  # type: ignore[arg-type]
    paths["catalog"].write_text(render_catalog(occurrences, logical), encoding="utf-8")  # type: ignore[arg-type]
    paths["dispositions"].write_text(render_dispositions(dispositions), encoding="utf-8")  # type: ignore[arg-type]
    paths["evidence"].write_text(render_evidence(evidence), encoding="utf-8")  # type: ignore[arg-type]
    paths["proposal"].write_text(render_proposal(proposal), encoding="utf-8")  # type: ignore[arg-type]

    loaded_manifest = load_manifest(paths["manifest"])
    loaded_occurrences, loaded_logical = load_catalog(paths["catalog"])
    loaded_dispositions = load_dispositions(paths["dispositions"])
    loaded_evidence = load_evidence(paths["evidence"])
    loaded_proposal = load_proposal(paths["proposal"])
    validate_period_references(loaded_manifest, loaded_logical, (loaded_evidence,))
    assert {item.occurrence_id for item in loaded_occurrences} == set(loaded_logical[0].occurrence_ids)
    assert loaded_dispositions[0].logical_repository_id == loaded_logical[0].logical_repository_id
    assert set(loaded_evidence.commit_ids) >= {loaded_dispositions[0].commit_id}
    assert set(loaded_proposal.evidence_ids) == {loaded_evidence.evidence_id}


def test_atomic_replace_failure_preserves_accepted_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "record.md"
    target.write_bytes(b"accepted\n")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(portfolio_markdown.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        atomic_write(target, "replacement\n")
    assert target.read_bytes() == b"accepted\n"
    assert list(tmp_path.iterdir()) == [target]


def test_catalog_requires_exactly_one_resolution_mapping(complete_records: tuple[object, ...]) -> None:
    occurrences = complete_records[1]
    logical = complete_records[2]
    assert isinstance(occurrences, list)
    assert isinstance(logical, list)
    duplicate = replace(logical[0], logical_repository_id="r2")
    with pytest.raises(ValueError, match="exactly one"):
        render_catalog(occurrences, [*logical, duplicate])
    unresolved = RawRepositoryOccurrence("o2", Path("missing"), "standard", None, None, (), None, "inaccessible", "unresolved")
    invalid = replace(logical[0], occurrence_ids=("o1", "o2"))
    with pytest.raises(ValueError, match="unresolved"):
        render_catalog([*occurrences, unresolved], [invalid])


def test_evidence_explanations_render_as_markdown_sections(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    evidence = complete_records[4]
    assert isinstance(evidence, EvidenceRecord)
    text = render_evidence(evidence)
    frontmatter = text.split("---\n", 2)[1]
    assert "problem:" not in frontmatter
    assert "## Problem\n\nproblem\n" in text
    assert load_evidence(_write_fixture(text, tmp_path / "evidence.md")).problem == "problem"


def _write_fixture(text: str, path: Path) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_table_cells_preserve_meaningful_surrounding_whitespace(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    evidence = complete_records[4]
    assert isinstance(evidence, EvidenceRecord)
    changed = replace(evidence, links=(replace(evidence.links[0], symbol_or_test="  parse  "), *evidence.links[1:]))
    path = tmp_path / "evidence.md"
    path.write_text(render_evidence(changed), encoding="utf-8")
    assert load_evidence(path).links[0].symbol_or_test == "  parse  "


def test_blocking_publication_journal_rejects_reader_target(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    manifest = complete_records[0]
    assert isinstance(manifest, ResearchManifest)
    path = tmp_path / "manifest.md"
    path.write_text(render_manifest(manifest), encoding="utf-8")
    write = PendingWrite(Path("manifest.md"), "before", "after", sha256(b"before").hexdigest(), sha256(b"after").hexdigest())
    journal = PublicationResult("tx-1", "manual-recovery", (replace(write, state="manual-recovery"),), Path("transactions/tx-1.md"))
    with pytest.raises(ValueError, match="manual recovery"):
        ensure_readable(Path("manifest.md"), (journal,))
    with pytest.raises(ValueError, match="manual recovery"):
        load_manifest(path, journals=(journal,))


def test_publication_journal_round_trips_and_unfinished_state_blocks(tmp_path: Path) -> None:
    write = PendingWrite(Path("manifest.md"), None, "new", None, sha256(b"new").hexdigest())
    journal = PublicationResult("tx-1", "prepared", (write,), Path("transactions/tx-1.md"))
    text = render_publication_result(journal)
    path = tmp_path / "tx-1.md"
    path.write_text(text, encoding="utf-8")
    loaded = load_publication_result(path)
    assert loaded == journal
    assert render_publication_result(loaded) == text
    with pytest.raises(ValueError, match="blocking publication journal"):
        ensure_readable(Path("manifest.md"), (loaded,))


def test_atomic_write_reports_uncertain_commit_after_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "record.md"
    target.write_text("old\n", encoding="utf-8")
    real_fsync = portfolio_markdown.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(portfolio_markdown.os, "fsync", fail_directory_fsync)
    with pytest.raises(portfolio_markdown.AtomicWriteCommitUncertain) as error:
        atomic_write(target, "new\n")
    assert error.value.output_digest == sha256(b"new\n").hexdigest()
    assert target.read_text(encoding="utf-8") == "new\n"


def test_fixed_table_rejects_unconsumed_content(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    evidence = complete_records[4]
    assert isinstance(evidence, EvidenceRecord)
    path = tmp_path / "evidence.md"
    path.write_text(render_evidence(evidence) + "\nignored content\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unconsumed"):
        load_evidence(path)


def test_catalog_round_trips_detached_head_pseudo_ref(tmp_path: Path, complete_records: tuple[object, ...]) -> None:
    occurrences = complete_records[1]
    logical = complete_records[2]
    assert isinstance(occurrences, list)
    assert isinstance(logical, list)
    detached_head = "HEAD=" + "b" * 40
    changed_occurrences = [replace(occurrences[0], observed_refs=(*occurrences[0].observed_refs, detached_head))]
    changed_logical = [replace(logical[0], observed_refs=(*logical[0].observed_refs, detached_head))]
    text = render_catalog(changed_occurrences, changed_logical)
    path = tmp_path / "catalog.md"
    path.write_text(text, encoding="utf-8")
    loaded = load_catalog(path)
    assert loaded == (changed_occurrences, changed_logical)
    assert render_catalog(*loaded) == text
