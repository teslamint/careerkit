from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Any, cast

import pytest
import yaml

from careerkit.jobs.adapters import semantic_eval_files
from careerkit.jobs.adapters.semantic_eval_files import SemanticEvalFileStore
from careerkit.jobs.application.semantic_eval import (
    FAMILY_AGGREGATION_CONTRACT_DIGEST,
    _redact_command,
    GOLD_SCHEMA,
    QUEUE_SCHEMA,
    REQUIRED_SLICES,
    REPORT_SCHEMA,
    SCORE_CONTRACT_DIGEST,
    SemanticModelProvenance,
    SemanticEvalCaptureSink,
    SemanticTitleScore,
    aggregate_report_view,
    authoritative_split,
    build_gold_dataset,
    build_semantic_capture_queue,
    compare_reports,
    compute_lock_digest,
    evaluate_dataset,
    load_dataset_payload,
    load_comparison_report_payload,
    load_eval_report_payload,
    load_queue_payload,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "jobs" / "semantic_filter_eval.json"
WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
EXPECTED_CACHE_NAMESPACE = "semantic-model-v2"
EXPECTED_MODEL_REVISION = "8fca7c9c98c26599be0e14b9916b11a756a26f19"


class StubScorer:
    def __init__(self, scores: dict[str, SemanticTitleScore], provenance: SemanticModelProvenance) -> None:
        self._scores = scores
        self._provenance = provenance
        self.close_calls = 0

    def prepare(self) -> None:
        return None

    def score_title(self, title: str) -> SemanticTitleScore:
        return self._scores[title]

    def provenance(self) -> SemanticModelProvenance:
        return self._provenance

    def close(self) -> None:
        self.close_calls += 1


class PrepareFailScorer(StubScorer):
    def prepare(self) -> None:
        raise RuntimeError("prepare failed")


class ProvenanceFailScorer(StubScorer):
    def provenance(self) -> SemanticModelProvenance:
        raise RuntimeError("provenance failed")


class CloseFailScorer(StubScorer):
    def __init__(self, scores: dict[str, SemanticTitleScore], provenance: SemanticModelProvenance, *, body_fails: bool = False) -> None:
        super().__init__(scores, provenance)
        self.body_fails = body_fails

    def score_title(self, title: str) -> SemanticTitleScore:
        if self.body_fails:
            raise ValueError("body failed")
        return super().score_title(title)

    def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("close failed")


class Clock:
    def __init__(self, points: list[float]) -> None:
        self._points = iter(points)

    def __call__(self) -> float:
        return next(self._points)


def _provenance(*, dataset_version: str = "2026-08-10", score_contract: str = SCORE_CONTRACT_DIGEST) -> SemanticModelProvenance:
    return SemanticModelProvenance(
        model_name="jhgan/ko-sroberta-multitask",
        model_revision="8fca7c9c98c26599be0e14b9916b11a756a26f19",
        sentence_transformers_version="5.1.0",
        anchor_digest="anchor-digest",
        keyword_override_digest="keyword-digest",
        dataset_digest="placeholder-dataset",
        split_digest="placeholder-split",
        family_lock_digest="placeholder-family-lock",
        git_sha="a" * 40,
        command="career-jobs semantic-eval run --dataset private/jd/evals/semantic-filter/gold.json --output private/jd/evals/semantic-filter/reports/incumbent.json --json",
        score_contract_digest=score_contract,
        family_aggregation_contract_digest=FAMILY_AGGREGATION_CONTRACT_DIGEST,
        dataset_schema=GOLD_SCHEMA,
        dataset_version=dataset_version,
    )


def _score(title: str, *, reject: bool, relative_score: float | None = None) -> SemanticTitleScore:
    return SemanticTitleScore(
        title=title,
        normalized_title=" ".join(title.casefold().split()),
        backend_score=0.8 if not reject else 0.2,
        non_backend_score=0.2 if not reject else 0.8,
        relative_score=(-0.25 if reject else 0.25) if relative_score is None else relative_score,
        reject=reject,
    )


def _case(case_id: str, family_id: str, title: str, label: str | None, slices: list[str]) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "family_id": family_id,
        "title": title,
        "label": label,
        "split": authoritative_split("2026-08-10", family_id),
        "slices": slices,
        "quick_filter_outcome": "eligible",
        "quick_filter_config_digest": "quick-filter-v1",
    }


def _queue_payload(cases: list[dict[str, Any]], digest: str = "queue-a") -> dict[str, Any]:
    return {
        "schema": QUEUE_SCHEMA,
        "source_queue_digest": digest,
        "captured_at": "2026-08-10T01:00:00Z",
        "cases": cases,
    }


def _gold_payload(cases: list[dict[str, Any]], *, evidence_tier: str = "synthetic", digests: list[str] | None = None) -> dict[str, Any]:
    use_digests = ["queue-a"] if digests is None else digests
    dataset = load_dataset_payload(
        {
            "schema": GOLD_SCHEMA,
            "dataset_version": "2026-08-10",
            "source_queue_digests": use_digests,
            "human_confirmation": {"confirmed_by": "tester", "confirmed_at": "2026-08-10T02:00:00Z"},
            "lock_timestamp": "2026-08-10T02:30:00Z",
            "lock_digest": "temporary",
            "evidence_tier": "synthetic",
            "cases": cases,
        }
    )
    lock_digest = compute_lock_digest(use_digests, dataset.cases)
    return {
        "schema": GOLD_SCHEMA,
        "dataset_version": "2026-08-10",
        "source_queue_digests": use_digests,
        "human_confirmation": {"confirmed_by": "tester", "confirmed_at": "2026-08-10T02:00:00Z"},
        "lock_timestamp": "2026-08-10T02:30:00Z",
        "lock_digest": lock_digest,
        "evidence_tier": evidence_tier,
        "cases": cases,
    }


def _report_payload(report: Any) -> dict[str, Any]:
    return {
        **aggregate_report_view(report),
        "schema": REPORT_SCHEMA,
        "family_results": [item.__dict__ for item in report.family_results],
        "case_scores": [
            {
                "case_id": item.case_id,
                "family_id": item.family_id,
                "split": item.split,
                "label": item.label,
                "slices": list(item.slices),
                "quick_filter_outcome": item.quick_filter_outcome,
                "quick_filter_config_digest": item.quick_filter_config_digest,
                "score": item.score.__dict__,
            }
            for item in report.case_scores
        ],
    }


def _fixture_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def test_queue_null_label_allowed_but_gold_requires_final_label() -> None:
    queue = load_queue_payload(_queue_payload([_case("q1", "fam-1", "Backend Engineer", None, ["english", "generic_software"])]))
    assert queue.cases[0].label is None
    with pytest.raises(ValueError, match="gold label is required"):
        load_dataset_payload(_gold_payload([_case("q1", "fam-1", "Backend Engineer", None, ["english", "generic_software"])]))


def test_gold_canonicalization_uses_nfkc_authoritative_split_and_conflict_rejection() -> None:
    dataset = load_dataset_payload(_gold_payload([_case("c1", "fam-2", "ＡI　Engineer", "non_backend", ["mixed", "ai_ml"])]))
    assert dataset.cases[0].normalized_title == "ai engineer"

    wrong_split = _case("c2", "fam-3", "Backend Engineer", "backend", ["english", "generic_software"])
    wrong_split["split"] = "holdout" if wrong_split["split"] == "calibration" else "calibration"
    with pytest.raises(ValueError, match="authoritative split mismatch"):
        load_dataset_payload(_gold_payload([wrong_split]))

    conflicting = [
        _case("c3", "fam-4", "Backend Engineer", "backend", ["english", "generic_software"]),
        _case("c4", "fam-5", "backend engineer", "non_backend", ["english", "generic_software"]),
    ]
    with pytest.raises(ValueError, match="conflicting normalized duplicate"):
        load_dataset_payload(_gold_payload(conflicting))


def test_exact_bound_and_unknown_contract_gate_status() -> None:
    small = load_dataset_payload(_gold_payload([_case("c1", "fam-10", "Backend Engineer", "backend", ["english", "generic_software"])]))
    small_report = evaluate_dataset(small, StubScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()), lambda: {"peak_rss_bytes": None}, Clock([0.0, 0.1, 0.2]))
    assert small_report.status == "insufficient_data"

    holdout_ids: list[int] = []
    index = 0
    while len(holdout_ids) < 299:
        if authoritative_split("2026-08-10", f"fam-{index}") == "holdout":
            holdout_ids.append(index)
        index += 1
    cases = [_case(f"h{i}", f"fam-{i}", f"Backend {i}", "backend", ["english", "generic_software"]) for i in holdout_ids]
    dataset = load_dataset_payload(_gold_payload(cases, evidence_tier="private_gold_locked"))
    report = evaluate_dataset(dataset, StubScorer({case["title"]: _score(case["title"], reject=False) for case in cases}, _provenance()), lambda: {"peak_rss_bytes": None}, Clock([0.0, 0.1] + [0.2 + 0.001 * i for i in range(299)]))
    assert report.status == "pass"
    assert report.authorizes_production_change is True

    unknown = evaluate_dataset(dataset, StubScorer({case["title"]: _score(case["title"], reject=False) for case in cases}, _provenance(score_contract="unknown-contract")), lambda: {"peak_rss_bytes": None}, Clock([0.0, 0.1] + [0.2 + 0.001 * i for i in range(299)]))
    assert unknown.status == "unavailable"
    assert unknown.authorizes_production_change is False


def test_large_n_confidence_and_observed_reject_slice_metrics() -> None:
    ids: list[int] = []
    index = 3000
    while len(ids) < 20000:
        if authoritative_split("2026-08-10", f"fam-{index}") == "holdout":
            ids.append(index)
        index += 1
    cases = [_case(f"c{i}", f"fam-{i}", f"Backend {i}", "backend", ["english", "generic_software"]) for i in ids]
    dataset = load_dataset_payload(_gold_payload(cases))
    report = evaluate_dataset(dataset, StubScorer({case["title"]: _score(case["title"], reject=False) for case in cases}, _provenance()), lambda: {"peak_rss_bytes": None}, Clock([0.0, 0.1] + [0.2 + 0.00001 * i for i in range(len(cases))]))
    assert report.confidence_intervals["keep_family_miss_rate_upper_bound"] < 0.001

    mixed = load_dataset_payload(_gold_payload([
        _case("m1", "fam-m1", "Backend Engineer", "backend", ["english", "generic_software"]),
        _case("m2", "fam-m2", "Data Engineer", "non_backend", ["english", "data"]),
        _case("m3", "fam-m3", "백엔드/AI 엔지니어", "ambiguous", ["mixed", "korean", "ai_ml"]),
    ]))
    mixed_report = evaluate_dataset(mixed, StubScorer({
        "Backend Engineer": _score("Backend Engineer", reject=False),
        "Data Engineer": _score("Data Engineer", reject=True),
        "백엔드/AI 엔지니어": _score("백엔드/AI 엔지니어", reject=True),
    }, _provenance()), lambda: {"peak_rss_bytes": 123}, Clock([0.0, 0.2, 0.4, 0.7, 1.0]))
    assert mixed_report.metrics["screening_calls_avoided_per_1000_titles"] == pytest.approx(666.6666667)
    assert mixed_report.slices["frontend"]["cases"] == 0
    assert mixed_report.slices["english"]["families"] == 2


def test_build_gold_dataset_uses_final_lock_after_deduplication() -> None:
    queue_a = load_queue_payload(_queue_payload([_case("q1", "fam-20", "Backend Engineer", None, ["english", "generic_software"])], "queue-a"))
    queue_b = load_queue_payload(_queue_payload([_case("q2", "fam-21", "backend engineer", None, ["english", "generic_software"])], "queue-b"))
    gold = build_gold_dataset(queue_payloads=[queue_a, queue_b], gold_payload=_gold_payload([
        _case("g1", "fam-20", "Backend Engineer", "backend", ["english", "generic_software"]),
        _case("g2", "fam-20", "backend engineer", "backend", ["english", "generic_software"]),
    ], digests=["queue-a", "queue-b"]))
    assert gold.queue_case_count == 1
    assert gold.locked is True


def test_build_semantic_capture_queue_collapses_exact_duplicates_and_keeps_related_variants() -> None:
    queue = build_semantic_capture_queue(
        titles=[
            {
                "title": "Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
            {
                "title": "backend engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
            {
                "title": "[Platform] Senior Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
        ],
        source_outcomes={"wanted": {"complete": True, "stop_reason": None, "pages_fetched": 2}},
        seed=13,
        captured_at="2026-08-11T00:00:00Z",
    )

    assert queue.schema == QUEUE_SCHEMA
    assert {case.title for case in queue.cases} == {
        "Backend Engineer",
        "[Platform] Senior Backend Engineer",
    }
    assert queue.cases[0].family_id == queue.cases[1].family_id
    assert all(case.label is None for case in queue.cases)
    provenance = cast(dict[str, object], queue.capture_provenance["platforms"])
    wanted = cast(dict[str, object], provenance["wanted"])
    assert wanted["pages_fetched"] == 2


def test_build_semantic_capture_queue_same_seed_is_stable_for_reversed_input_order() -> None:
    forward = build_semantic_capture_queue(
        titles=[
            {
                "title": "Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
            {
                "title": "[Platform] Senior Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
        ],
        source_outcomes={"wanted": {"complete": True, "stop_reason": None, "pages_fetched": 2}},
        seed=13,
        captured_at="2026-08-11T00:00:00Z",
    )
    reversed_input = build_semantic_capture_queue(
        titles=[
            {
                "title": "[Platform] Senior Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
            {
                "title": "Backend Engineer",
                "quick_filter_outcome": "eligible",
                "quick_filter_config_digest": "quick-filter-v1",
            },
        ],
        source_outcomes={"wanted": {"complete": True, "stop_reason": None, "pages_fetched": 2}},
        seed=13,
        captured_at="2026-08-11T00:00:00Z",
    )

    assert [case.title for case in forward.cases] == [case.title for case in reversed_input.cases]
    assert [case.case_id for case in forward.cases] == [case.case_id for case in reversed_input.cases]
    assert forward.source_queue_digest == reversed_input.source_queue_digest


def test_semantic_eval_file_store_writes_owner_only_json_without_clobber(tmp_path) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"

    written = store.write_new_json(
        path,
        {
            "schema": QUEUE_SCHEMA,
            "source_queue_digest": "queue-a",
            "captured_at": "2026-08-11T00:00:00Z",
            "cases": [],
        },
        purpose="semantic queue",
    )

    assert written == path
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert store.read_json(path, purpose="semantic queue")["schema"] == QUEUE_SCHEMA
    with pytest.raises(FileExistsError):
        store.write_new_json(
            path,
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-b",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )


def test_semantic_eval_file_store_rejects_escape_and_symlink_parent(tmp_path) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    outside = tmp_path.parent / "outside.json"
    with pytest.raises(ValueError, match="inside an allowed root"):
        store.write_new_json(
            tmp_path / ".." / "outside.json",
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-a",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )

    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.write_new_json(
            linked / "queue.json",
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-a",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )
    assert not outside.exists()


def test_semantic_eval_file_store_rejects_git_tracked_workspace_output_before_creating_temp_files(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(mode=0o700)
    tracked_dir = repo_root / "reports"
    tracked_dir.mkdir(mode=0o700)
    tracked_file = tracked_dir / "queue.json"
    tracked_file.write_text('{"tracked": true}\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(["git", "-C", str(repo_root), "add", "reports/queue.json"], check=True)
    store = SemanticEvalFileStore(allowed_roots=(repo_root,))

    before = sorted(path.name for path in tracked_dir.iterdir())
    with pytest.raises(ValueError, match="tracked git path"):
        store.write_new_json(
            tracked_file,
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-a",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )

    assert tracked_file.read_text(encoding="utf-8") == '{"tracked": true}\n'
    assert sorted(path.name for path in tracked_dir.iterdir()) == before


def test_semantic_eval_file_store_cleans_owned_orphans_and_allows_one_concurrent_publish(tmp_path, monkeypatch) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"
    path.parent.mkdir(mode=0o700)
    orphan = path.parent / f".{path.name}.tmp-orphan"
    orphan.write_text("{}", encoding="utf-8")
    orphan.chmod(0o600)
    stale = time.time() - semantic_eval_files.ORPHAN_MIN_AGE_SECONDS - 60
    os.utime(orphan, (stale, stale))

    both_at_link = threading.Barrier(2, timeout=30)
    original_link = __import__("careerkit.jobs.adapters.semantic_eval_files", fromlist=["os"]).os.link

    def synchronized_link(*args, **kwargs):
        both_at_link.wait()
        return original_link(*args, **kwargs)

    monkeypatch.setattr("careerkit.jobs.adapters.semantic_eval_files.os.link", synchronized_link)

    results: list[object] = []

    def writer(digest: str) -> None:
        try:
            results.append(
                store.write_new_json(
                    path,
                    {
                        "schema": QUEUE_SCHEMA,
                        "source_queue_digest": digest,
                        "captured_at": "2026-08-11T00:00:00Z",
                        "cases": [],
                    },
                    purpose="semantic queue",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            results.append(exc)

    left = threading.Thread(target=writer, args=("queue-left",))
    right = threading.Thread(target=writer, args=("queue-right",))
    left.start()
    right.start()
    left.join()
    right.join()

    assert orphan.exists() is False
    assert sum(1 for item in results if item == path) == 1
    assert sum(1 for item in results if isinstance(item, FileExistsError)) == 1


def test_semantic_eval_file_store_orphan_cleanup_spares_a_temp_another_publish_holds(tmp_path, monkeypatch) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"
    path.parent.mkdir(mode=0o700)

    temp_created = threading.Event()
    cleanup_done = threading.Event()
    original_mkstemp = semantic_eval_files.tempfile.mkstemp
    original_cleanup = SemanticEvalFileStore._cleanup_orphans

    def staggered_mkstemp(*args, **kwargs):
        result = original_mkstemp(*args, **kwargs)
        if threading.current_thread().name == "holder":
            temp_created.set()
            assert cleanup_done.wait(timeout=30)
        return result

    def staggered_cleanup(self, parent_path, leaf_name):
        if threading.current_thread().name == "collector":
            assert temp_created.wait(timeout=30)
        original_cleanup(self, parent_path, leaf_name)
        if threading.current_thread().name == "collector":
            cleanup_done.set()

    monkeypatch.setattr("careerkit.jobs.adapters.semantic_eval_files.tempfile.mkstemp", staggered_mkstemp)
    monkeypatch.setattr(SemanticEvalFileStore, "_cleanup_orphans", staggered_cleanup)

    results: list[object] = []

    def writer(digest: str) -> None:
        try:
            results.append(
                store.write_new_json(
                    path,
                    {
                        "schema": QUEUE_SCHEMA,
                        "source_queue_digest": digest,
                        "captured_at": "2026-08-11T00:00:00Z",
                        "cases": [],
                    },
                    purpose="semantic queue",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            results.append(exc)

    holder = threading.Thread(target=writer, args=("queue-holder",), name="holder")
    collector = threading.Thread(target=writer, args=("queue-collector",), name="collector")
    holder.start()
    collector.start()
    holder.join()
    collector.join()

    assert sum(1 for item in results if item == path) == 1
    assert sum(1 for item in results if isinstance(item, FileExistsError)) == 1
    assert [item for item in results if isinstance(item, FileNotFoundError)] == []
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_semantic_eval_file_store_injected_pre_publish_failure_keeps_target_absent_and_cleans_temp(tmp_path, monkeypatch) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"

    def fail_link(*args, **kwargs):
        raise RuntimeError("injected publish failure")

    monkeypatch.setattr("careerkit.jobs.adapters.semantic_eval_files.os.link", fail_link)

    with pytest.raises(RuntimeError, match="injected publish failure"):
        store.write_new_json(
            path,
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-a",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )

    assert path.exists() is False
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_semantic_eval_file_store_keyboard_interrupt_cleanup_removes_temp_and_target(tmp_path, monkeypatch) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"

    def interrupt_link(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr("careerkit.jobs.adapters.semantic_eval_files.os.link", interrupt_link)

    with pytest.raises(KeyboardInterrupt):
        store.write_new_json(
            path,
            {
                "schema": QUEUE_SCHEMA,
                "source_queue_digest": "queue-a",
                "captured_at": "2026-08-11T00:00:00Z",
                "cases": [],
            },
            purpose="semantic queue",
        )

    assert path.exists() is False
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_semantic_eval_file_store_sigterm_cleanup_removes_temp_and_preserves_target_absence(tmp_path, monkeypatch) -> None:
    store = SemanticEvalFileStore(allowed_roots=(tmp_path,))
    path = tmp_path / "semantic" / "queue.json"

    class Terminated(BaseException):
        pass

    def raise_term(_signum, _frame):
        raise Terminated()

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, raise_term)

    def term_link(*args, **kwargs):
        signal.raise_signal(signal.SIGTERM)
        raise AssertionError("unreachable")

    monkeypatch.setattr("careerkit.jobs.adapters.semantic_eval_files.os.link", term_link)

    try:
        with pytest.raises(Terminated):
            store.write_new_json(
                path,
                {
                    "schema": QUEUE_SCHEMA,
                    "source_queue_digest": "queue-a",
                    "captured_at": "2026-08-11T00:00:00Z",
                    "cases": [],
                },
                purpose="semantic queue",
            )
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert path.exists() is False
    assert list(path.parent.glob(f".{path.name}.tmp-*")) == []


def test_semantic_eval_capture_sink_publishes_empty_complete_queue_and_blocks_incomplete_publish(tmp_path) -> None:
    sink = SemanticEvalCaptureSink(
        output_path=tmp_path / "semantic" / "queue.json",
        allowed_roots=(tmp_path,),
        seed=17,
    )
    sink.record_source_outcome("wanted", complete=True, stop_reason=None, pages_fetched=0)

    written = sink.publish(captured_at="2026-08-11T00:00:00Z")

    payload = load_queue_payload(SemanticEvalFileStore(allowed_roots=(tmp_path,)).read_json(written, purpose="semantic queue"))
    assert payload.cases == ()
    platforms = cast(dict[str, object], payload.capture_provenance["platforms"])
    wanted = cast(dict[str, object], platforms["wanted"])
    assert wanted["complete"] is True

    blocked_sink = SemanticEvalCaptureSink(
        output_path=tmp_path / "semantic" / "blocked.json",
        allowed_roots=(tmp_path,),
        seed=19,
    )
    blocked_sink.record_source_outcome("wanted", complete=False, stop_reason="request_error", pages_fetched=0)
    blocked_sink.mark_incomplete("platform_failure")
    with pytest.raises(ValueError, match="platform_failure"):
        blocked_sink.publish(captured_at="2026-08-11T00:00:00Z")
    assert (tmp_path / "semantic" / "blocked.json").exists() is False


def test_report_round_trip_recomputes_and_redacts() -> None:
    dataset = load_dataset_payload(_gold_payload([_case("r1", "fam-30", "Backend Engineer", "backend", ["english", "generic_software"])], evidence_tier="private_gold_locked"))
    report = evaluate_dataset(dataset, StubScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()), lambda: {"peak_rss": 64, "platform": "darwin", "unit": "bytes"}, Clock([0.0, 0.1, 0.2]))
    payload = _report_payload(report)
    loaded = load_eval_report_payload(payload, dataset)
    assert loaded.authorizes_production_change is False
    public = aggregate_report_view(loaded)
    provenance = public["provenance"]
    assert isinstance(provenance, dict)
    command = provenance["command"]
    assert isinstance(command, str)
    assert "private/jd/evals" not in command
    assert "<redacted>" in command

    false_auth = dict(payload)
    false_auth["authorizes_production_change"] = False
    loaded_false = load_eval_report_payload(false_auth, dataset)
    assert loaded_false.authorizes_production_change is False

    broken = _report_payload(report)
    broken["family_results"] = []
    with pytest.raises(ValueError, match="family evidence mismatch"):
        load_eval_report_payload(broken, dataset)


def test_redact_command_keeps_temp_paths_and_hides_workspace_private_paths() -> None:
    macos_temp = _redact_command(
        "career-jobs semantic-eval run"
        " --dataset /private/tmp/semantic-filter-eval/dataset.json"
        " --output /private/var/folders/x9/T/semantic-filter-eval/report.json --json"
    )
    assert "/private/tmp/semantic-filter-eval/dataset.json" in macos_temp
    assert "/private/var/folders/x9/T/semantic-filter-eval/report.json" in macos_temp
    assert "<redacted>" not in macos_temp

    runner_temp = _redact_command(
        "career-jobs semantic-eval run"
        " --dataset /home/runner/work/_temp/semantic-filter-eval/dataset.json"
        " --output /home/runner/work/_temp/semantic-filter-eval/report.json --json"
    )
    assert "semantic-filter-eval/dataset.json" in runner_temp
    assert "semantic-filter-eval/report.json" in runner_temp
    assert "private/" not in runner_temp

    workspace_private = _redact_command(
        "career-jobs semantic-eval run"
        " --dataset private/jd/evals/semantic-filter/gold.json"
        " --output=/private/tmp/workspace/private/jd/evals/semantic-filter/reports/incumbent.json --json"
    )
    assert "private/jd/evals" not in workspace_private
    assert workspace_private.count("<redacted>") == 2
    assert _redact_command(workspace_private) == workspace_private


def test_comparison_requires_authorized_candidate_exact_case_sets_and_strict_coherence() -> None:
    ids: list[int] = []
    index = 500
    while len(ids) < 299:
        if authoritative_split("2026-08-10", f"fam-{index}") == "holdout":
            ids.append(index)
        index += 1
    backend_cases = [_case(f"b{i}", f"fam-{i}", f"Backend {i}", "backend", ["english", "generic_software"]) for i in ids]
    reject_cases = [_case(f"d{i}", f"rfam-{i}", f"Data {i}", "non_backend", ["english", "data"]) for i in range(10)]
    dataset = load_dataset_payload(_gold_payload(backend_cases + reject_cases, evidence_tier="private_gold_locked"))
    incumbent_scores = {case["title"]: _score(case["title"], reject=False) for case in backend_cases + reject_cases}
    candidate_scores = dict(incumbent_scores)
    for i in range(6):
        candidate_scores[f"Data {i}"] = _score(f"Data {i}", reject=True)
    incumbent = evaluate_dataset(dataset, StubScorer(incumbent_scores, _provenance()), lambda: {"peak_rss": 100, "platform": "linux", "unit": "KiB"}, Clock([0.0, 0.1] + [0.2 + 0.001 * i for i in range(len(dataset.cases))]))
    candidate = evaluate_dataset(dataset, StubScorer(candidate_scores, _provenance()), lambda: {"peak_rss_bytes": 128}, Clock([0.0, 0.1] + [0.2 + 0.001 * i for i in range(len(dataset.cases))]))
    unauthorized = candidate.__class__(
        schema=candidate.schema,
        status=candidate.status,
        evidence_tier=candidate.evidence_tier,
        authorizes_production_change=False,
        provenance=candidate.provenance,
        counts=candidate.counts,
        metrics=candidate.metrics,
        confidence_intervals=candidate.confidence_intervals,
        slices=candidate.slices,
        latency=candidate.latency,
        resource_cost=candidate.resource_cost,
        family_results=candidate.family_results,
        case_scores=candidate.case_scores,
        lock_validated=candidate.lock_validated,
    )
    comparison = compare_reports(dataset, incumbent, unauthorized)
    assert comparison.comparison["reason"] == "candidate_not_authorized"

    missing_case = _report_payload(candidate)
    missing_case["case_scores"] = missing_case["case_scores"][:-1]
    with pytest.raises(ValueError, match="case evidence mismatch"):
        load_eval_report_payload(missing_case, dataset)



def test_load_eval_report_rejects_provenance_dataset_contract_mismatch() -> None:
    dataset = load_dataset_payload(_gold_payload([_case("p1", "fam-60", "Backend Engineer", "backend", ["english", "generic_software"])]))
    report = evaluate_dataset(
        dataset,
        StubScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()),
        lambda: {"peak_rss_bytes": 64},
        Clock([0.0, 0.1, 0.2]),
    )
    payload = _report_payload(report)
    cases = [
        ("dataset_schema", "other-schema", "dataset schema mismatch"),
        ("dataset_version", "2026-08-09", "dataset version mismatch"),
        ("dataset_digest", "bad-digest", "dataset digest mismatch"),
        ("split_digest", "bad-split", "split digest mismatch"),
        ("family_lock_digest", "bad-family-lock", "family lock digest mismatch"),
        ("score_contract_digest", "bad-score-contract", "score contract digest mismatch"),
        ("family_aggregation_contract_digest", "bad-family-contract", "family aggregation contract digest mismatch"),
    ]
    for field, value, message in cases:
        mutated = dict(payload)
        provenance = dict(mutated["provenance"])
        provenance[field] = value
        mutated["provenance"] = provenance
        try:
            load_eval_report_payload(mutated, dataset)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"expected mismatch for {field}")


def test_synthetic_fixture_covers_required_slices_without_private_fields_and_stays_non_authorizing() -> None:
    payload = _fixture_payload()
    assert payload["schema"] == GOLD_SCHEMA
    assert payload["evidence_tier"] == "synthetic"
    dataset = load_dataset_payload(payload)

    cases = cast(list[dict[str, Any]], payload["cases"])
    assert {case["label"] for case in cases} == {"backend", "ambiguous", "non_backend"}
    assert {case["split"] for case in cases} == {"calibration", "holdout"}
    assert {
        slice_name
        for case in cases
        for slice_name in cast(list[str], case["slices"])
    } == set(REQUIRED_SLICES)

    forbidden_keys = {"platform", "company", "team", "screening", "screening_verdict", "employment_type"}
    assert forbidden_keys.isdisjoint(payload)
    for case in cases:
        assert forbidden_keys.isdisjoint(case)
        assert case["split"] == authoritative_split(payload["dataset_version"], case["family_id"])

    scores = {
        "[시니어] 백엔드 엔지니어": _score("[시니어] 백엔드 엔지니어", reject=False),
        "백엔드 ML Engineer": _score("백엔드 ML Engineer", reject=False, relative_score=0.01),
        "Senior Frontend QA Data DevOps Embedded Engineer": _score(
            "Senior Frontend QA Data DevOps Embedded Engineer",
            reject=True,
        ),
    }
    report = evaluate_dataset(dataset, StubScorer(scores, _provenance()), lambda: {"peak_rss_bytes": 64}, Clock([0.0, 0.1, 0.2, 0.3, 0.4]))
    assert report.status == "insufficient_data"
    assert report.authorizes_production_change is False
    assert report.counts["total_cases"] == 3
    assert report.counts["holdout_cases"] == 2
    assert report.counts["calibration_cases"] == 1


def test_ci_workflow_gates_semantic_eval_with_pinned_public_cache_and_temp_outputs() -> None:
    workflow = cast(dict[str, Any], yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8")))

    assert workflow["permissions"] == {"contents": "read"}
    jobs = cast(dict[str, Any], workflow["jobs"])
    test_job = cast(dict[str, Any], jobs["test"])
    job_env = cast(dict[str, str], test_job["env"])
    assert job_env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1"
    assert all("${{ runner." not in value for value in job_env.values())

    steps = cast(list[dict[str, Any]], test_job["steps"])
    path_step = next(step for step in steps if step.get("name") == "Configure semantic model paths")
    path_run = cast(str, path_step["run"])
    assert 'HF_HOME=$RUNNER_TEMP/hf-home' in path_run
    assert 'HF_PUBLIC_CACHE=$RUNNER_TEMP/public-hf-models' in path_run
    assert '>> "$GITHUB_ENV"' in path_run

    cache_step = next(step for step in steps if step.get("uses", "").startswith("actions/cache@"))
    assert cache_step["uses"] == "actions/cache@27d5ce7f107fe9357f9df03efb73ab90386fccae"
    cache_with = cast(dict[str, Any], cache_step["with"])
    assert "models--jhgan--ko-sroberta-multitask/blobs" in cache_with["path"]
    assert f"models--jhgan--ko-sroberta-multitask/snapshots/{EXPECTED_MODEL_REVISION}" in cache_with["path"]
    assert EXPECTED_CACHE_NAMESPACE in cache_with["key"]
    assert EXPECTED_MODEL_REVISION in cache_with["key"]

    step_runs = "\n".join(cast(str, step.get("run", "")) for step in steps)
    assert "uv run pytest tests/jobs/test_semantic_eval.py tests/jobs/test_semantic_filter.py -q" in step_runs
    assert 'install -d -m 700 "$report_dir"' in step_runs
    assert 'dataset_path="$report_dir/dataset.json"' in step_runs
    assert 'install -m 600 tests/fixtures/jobs/semantic_filter_eval.json "$dataset_path"' in step_runs
    assert 'career-jobs --workspace tests/fixtures/workspace/basic semantic-eval run --dataset "$dataset_path"' in step_runs
    assert '--output "$report_path"' in step_runs
    assert "HF_HOME=\"$RUNNER_TEMP/hf-home\"" in step_runs
    assert "grep -q '\"status\": \"insufficient_data\"'" in step_runs
    assert "unexpected semantic-eval exit 2" in step_runs
    assert "authorizes_production_change" in step_runs
    assert "token" in step_runs
    assert "unexpected model repository" in step_runs
    assert "unexpected lock repository" in step_runs
    assert 'hub_metadata_entries = {".locks", "CACHEDIR.TAG", "version.txt"}' in step_runs
    assert 'repo_entries - {"blobs", "snapshots", ".no_exist"}' in step_runs
    assert "snapshot files must be symlinks into blobs" in step_runs
    assert "unexpected extra snapshot directories" in step_runs
    assert "unexpected extra blobs" in step_runs
    assert "refs directory is not allowed" in step_runs

def test_prepare_provenance_close_and_resource_failures() -> None:
    dataset = load_dataset_payload(_gold_payload([_case("z1", "fam-40", "Backend Engineer", "backend", ["english", "generic_software"])]))
    with pytest.raises(RuntimeError, match="prepare failed"):
        evaluate_dataset(dataset, PrepareFailScorer({}, _provenance()), lambda: {"peak_rss_bytes": 1}, Clock([0.0]))
    with pytest.raises(RuntimeError, match="provenance failed"):
        evaluate_dataset(dataset, ProvenanceFailScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()), lambda: {"peak_rss_bytes": 1}, Clock([0.0, 0.1]))
    with pytest.raises(ValueError, match="scorer close failed"):
        evaluate_dataset(dataset, CloseFailScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()), lambda: {"peak_rss_bytes": 1}, Clock([0.0, 0.1, 0.2]))
    with pytest.raises(ValueError, match="body failed") as excinfo:
        evaluate_dataset(dataset, CloseFailScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance(), body_fails=True), lambda: {"peak_rss_bytes": 1}, Clock([0.0, 0.1]))
    assert "close failure" in "\n".join(excinfo.value.__notes__)
    with pytest.raises(ValueError, match="peak_rss must be finite and non-negative"):
        evaluate_dataset(dataset, StubScorer({"Backend Engineer": _score("Backend Engineer", reject=False)}, _provenance()), lambda: {"peak_rss": -1, "platform": "linux", "unit": "KiB"}, Clock([0.0, 0.1, 0.2]))


def test_private_score_report_and_comparison_round_trip_keep_case_level_evidence() -> None:
    dataset = load_dataset_payload(_gold_payload([_case('case-private-1', 'family-private-1', 'SENTINEL PRIVATE BACKEND TITLE', 'backend', ['english', 'generic_software'])]))
    report = evaluate_dataset(
        dataset,
        StubScorer({'SENTINEL PRIVATE BACKEND TITLE': _score('SENTINEL PRIVATE BACKEND TITLE', reject=False)}, _provenance()),
        lambda: {'peak_rss_bytes': 64},
        Clock([0.0, 0.1, 0.2]),
    )
    report_payload = _report_payload(report)
    loaded = load_eval_report_payload(report_payload, dataset)
    assert loaded.case_scores[0].case_id == 'case-private-1'
    assert loaded.case_scores[0].score.title == 'SENTINEL PRIVATE BACKEND TITLE'

    comparison_payload = aggregate_report_view(compare_reports(dataset, loaded, loaded))
    comparison_payload['schema'] = 'semantic-filter-comparison-report/v1'
    comparison_payload['status'] = 'fail'
    comparison_payload['comparison'] = {'candidate_gains': 0, 'candidate_losses': 0, 'mcnemar_p_value': 1.0, 'reason': 'candidate_not_authorized'}
    loaded_comparison = compare_reports(dataset, loaded, loaded.__class__(
        schema=loaded.schema,
        status=loaded.status,
        evidence_tier=loaded.evidence_tier,
        authorizes_production_change=False,
        provenance=loaded.provenance,
        counts=loaded.counts,
        metrics=loaded.metrics,
        confidence_intervals=loaded.confidence_intervals,
        slices=loaded.slices,
        latency=loaded.latency,
        resource_cost=loaded.resource_cost,
        family_results=loaded.family_results,
        case_scores=loaded.case_scores,
        lock_validated=loaded.lock_validated,
    ))
    assert loaded_comparison.case_scores[0].case_id == 'case-private-1'
    assert loaded_comparison.comparison['reason'] == 'candidate_not_authorized'


def test_comparison_round_trip_binds_both_reports_and_rejects_digest_tampering() -> None:
    dataset = load_dataset_payload(_gold_payload([_case('case-private-1', 'family-private-1', 'SENTINEL PRIVATE BACKEND TITLE', 'backend', ['english', 'generic_software'])]))
    incumbent = evaluate_dataset(
        dataset,
        StubScorer({'SENTINEL PRIVATE BACKEND TITLE': _score('SENTINEL PRIVATE BACKEND TITLE', reject=False)}, _provenance()),
        lambda: {'peak_rss_bytes': 64},
        Clock([0.0, 0.1, 0.2]),
    )
    candidate = incumbent.__class__(
        schema=incumbent.schema,
        status=incumbent.status,
        evidence_tier=incumbent.evidence_tier,
        authorizes_production_change=False,
        provenance=incumbent.provenance,
        counts=incumbent.counts,
        metrics=incumbent.metrics,
        confidence_intervals=incumbent.confidence_intervals,
        slices=incumbent.slices,
        latency=incumbent.latency,
        resource_cost=incumbent.resource_cost,
        family_results=incumbent.family_results,
        case_scores=incumbent.case_scores,
        lock_validated=incumbent.lock_validated,
    )
    comparison = compare_reports(dataset, incumbent, candidate)
    payload = {
        **aggregate_report_view(comparison),
        'schema': 'semantic-filter-comparison-report/v1',
        'family_results': [item.__dict__ for item in comparison.family_results],
        'case_scores': [
            {
                'case_id': item.case_id,
                'family_id': item.family_id,
                'split': item.split,
                'label': item.label,
                'slices': list(item.slices),
                'quick_filter_outcome': item.quick_filter_outcome,
                'quick_filter_config_digest': item.quick_filter_config_digest,
                'score': item.score.__dict__,
            }
            for item in comparison.case_scores
        ],
        'comparison': dict(comparison.comparison),
    }
    payload['binding'] = {
        'dataset_digest': dataset.dataset_digest,
        'split_digest': dataset.split_digest,
        'family_lock_digest': dataset.family_lock_digest,
        'incumbent_report_digest': 'incumbent-digest',
        'candidate_report_digest': 'candidate-digest',
        'incumbent_provenance': aggregate_report_view(incumbent)['provenance'],
        'candidate_provenance': aggregate_report_view(candidate)['provenance'],
    }
    loaded = load_comparison_report_payload(
        payload,
        dataset,
        incumbent,
        candidate,
        incumbent_report_digest='incumbent-digest',
        candidate_report_digest='candidate-digest',
    )
    assert loaded.comparison['reason'] == 'candidate_not_authorized'

    tampered = dict(payload)
    tampered['binding'] = dict(payload['binding'])
    tampered['binding']['candidate_report_digest'] = 'different-digest'
    with pytest.raises(ValueError, match='candidate report digest mismatch'):
        load_comparison_report_payload(
            tampered,
            dataset,
            incumbent,
            candidate,
            incumbent_report_digest='incumbent-digest',
            candidate_report_digest='candidate-digest',
        )


def test_comparison_round_trip_validates_exact_shape_and_tampering() -> None:
    dataset = load_dataset_payload(_gold_payload([_case('case-private-1', 'family-private-1', 'SENTINEL PRIVATE BACKEND TITLE', 'backend', ['english', 'generic_software'])]))
    incumbent = evaluate_dataset(
        dataset,
        StubScorer({'SENTINEL PRIVATE BACKEND TITLE': _score('SENTINEL PRIVATE BACKEND TITLE', reject=False)}, _provenance()),
        lambda: {'peak_rss_bytes': 64},
        Clock([0.0, 0.1, 0.2]),
    )
    candidate = incumbent.__class__(
        schema=incumbent.schema,
        status=incumbent.status,
        evidence_tier=incumbent.evidence_tier,
        authorizes_production_change=False,
        provenance=incumbent.provenance,
        counts=incumbent.counts,
        metrics=incumbent.metrics,
        confidence_intervals=incumbent.confidence_intervals,
        slices=incumbent.slices,
        latency=incumbent.latency,
        resource_cost=incumbent.resource_cost,
        family_results=incumbent.family_results,
        case_scores=incumbent.case_scores,
        lock_validated=incumbent.lock_validated,
    )
    comparison = compare_reports(dataset, incumbent, candidate)
    payload = {
        **aggregate_report_view(comparison),
        'schema': 'semantic-filter-comparison-report/v1',
        'family_results': [item.__dict__ for item in comparison.family_results],
        'case_scores': [
            {
                'case_id': item.case_id,
                'family_id': item.family_id,
                'split': item.split,
                'label': item.label,
                'slices': list(item.slices),
                'quick_filter_outcome': item.quick_filter_outcome,
                'quick_filter_config_digest': item.quick_filter_config_digest,
                'score': item.score.__dict__,
            }
            for item in comparison.case_scores
        ],
        'comparison': dict(comparison.comparison),
        'binding': {
            'dataset_digest': dataset.dataset_digest,
            'split_digest': dataset.split_digest,
            'family_lock_digest': dataset.family_lock_digest,
            'incumbent_report_digest': 'incumbent-digest',
            'candidate_report_digest': 'candidate-digest',
            'incumbent_provenance': aggregate_report_view(incumbent)['provenance'],
            'candidate_provenance': aggregate_report_view(candidate)['provenance'],
        },
    }
    loaded = load_comparison_report_payload(
        payload, dataset, incumbent, candidate, incumbent_report_digest='incumbent-digest', candidate_report_digest='candidate-digest'
    )
    assert loaded.comparison['reason'] == 'candidate_not_authorized'

    extra = dict(payload)
    extra['extra_key'] = True
    with pytest.raises(ValueError, match='exact comparison report keys'):
        load_comparison_report_payload(extra, dataset, incumbent, candidate, incumbent_report_digest='incumbent-digest', candidate_report_digest='candidate-digest')

    bad_counts = dict(payload)
    bad_counts['counts'] = {'holdout_cases': 999}
    with pytest.raises(ValueError, match='count evidence mismatch'):
        load_comparison_report_payload(bad_counts, dataset, incumbent, candidate, incumbent_report_digest='incumbent-digest', candidate_report_digest='candidate-digest')

    bad_family = dict(payload)
    bad_family['family_results'] = []
    with pytest.raises(ValueError, match='family evidence mismatch'):
        load_comparison_report_payload(bad_family, dataset, incumbent, candidate, incumbent_report_digest='incumbent-digest', candidate_report_digest='candidate-digest')
