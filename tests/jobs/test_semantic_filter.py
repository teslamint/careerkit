from __future__ import annotations

import sys
from types import ModuleType

from careerkit.jobs.adapters.semantic_filter import (
    APPROVED_MODEL_REVISIONS,
    BACKEND_ANCHORS,
    DEFAULT_MODEL,
    KEYWORD_OVERRIDE_DIGEST,
    MODEL_THRESHOLDS,
    NON_BACKEND_ANCHORS,
    SemanticFilterAdapter,
    SemanticFilterCapability,
)
from careerkit.workspace import WorkspacePaths

EXPECTED_DEFAULT_ANCHOR_DIGEST = "457ea3f821ea9962dbdcd79d814c3828a460c1d4f318b40865e044697450dc8e"
EXPECTED_DEFAULT_CACHE_KEY = "bb94b19df269"


class StubImporter:
    def __init__(self, fail: bool = True):
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, name: str):
        self.calls.append(name)
        if self.fail:
            raise ImportError(name)
        module = ModuleType(name)
        return module


class BrokenModel:
    def __init__(self, _: str, revision: str | None = None) -> None:
        raise OSError("model unavailable offline")


class FakeVector(list[float]):
    def __itruediv__(self, scalar: float) -> FakeVector:
        for index, value in enumerate(self):
            self[index] = value / scalar
        return self


class FakeNumpy(ModuleType):
    def __init__(self) -> None:
        super().__init__("numpy")
        self.linalg = type("Linalg", (), {"norm": staticmethod(self.norm)})()

    @staticmethod
    def mean(rows: list[list[float]], axis: int = 0) -> FakeVector:
        if axis != 0:
            raise AssertionError("tests only support axis=0")
        width = len(rows[0])
        return FakeVector(
            [sum(row[index] for row in rows) / len(rows) for index in range(width)]
        )

    @staticmethod
    def dot(left: list[float], right: list[float]) -> float:
        return sum(left[index] * right[index] for index in range(len(left)))

    @staticmethod
    def norm(values: list[float]) -> float:
        return sum(value * value for value in values) ** 0.5


class FakeSentenceTransformer:
    init_calls: list[tuple[str, str | None]]
    scores: dict[str, list[float]]
    backend_anchor_vector: list[float]
    non_backend_anchor_vector: list[float]

    def __init__(self, model_name: str, revision: str | None = None) -> None:
        self.init_calls.append((model_name, revision))

    def encode(self, inputs: list[str], *, normalize_embeddings: bool) -> list[list[float]]:
        assert normalize_embeddings is True
        encoded: list[list[float]] = []
        for title in inputs:
            if title in BACKEND_ANCHORS:
                encoded.append(list(self.backend_anchor_vector))
                continue
            if title in NON_BACKEND_ANCHORS:
                encoded.append(list(self.non_backend_anchor_vector))
                continue
            encoded.append(list(self.scores.get(title, [0.0, 1.0])))
        return encoded


class FakeSentenceTransformers(ModuleType):
    def __init__(
        self,
        scores: dict[str, list[float]],
        *,
        version: str = "3.0.1",
        backend_anchor_vector: list[float] | None = None,
        non_backend_anchor_vector: list[float] | None = None,
    ) -> None:
        super().__init__("sentence_transformers")
        self.__version__ = version
        fake_model = type(
            "RecordingSentenceTransformer",
            (FakeSentenceTransformer,),
            {
                "init_calls": [],
                "scores": scores,
                "backend_anchor_vector": backend_anchor_vector or [1.0, 0.0],
                "non_backend_anchor_vector": non_backend_anchor_vector or [0.0, 1.0],
            },
        )
        self.SentenceTransformer = fake_model


def _semantic_modules(
    scores: dict[str, list[float]],
    *,
    version: str = "3.0.1",
    backend_anchor_vector: list[float] | None = None,
    non_backend_anchor_vector: list[float] | None = None,
) -> dict[str, ModuleType]:
    return {
        "sentence_transformers": FakeSentenceTransformers(
            scores,
            version=version,
            backend_anchor_vector=backend_anchor_vector,
            non_backend_anchor_vector=non_backend_anchor_vector,
        ),
        "numpy": FakeNumpy(),
    }


def _semantic_importer(scores: dict[str, list[float]]):
    modules = _semantic_modules(scores)

    def importer(name: str):
        return modules[name]

    return importer


class MutableImporter:
    def __init__(self, modules: dict[str, ModuleType]) -> None:
        self.modules = modules

    def __call__(self, name: str) -> ModuleType:
        return self.modules[name]


def test_semantic_filter_capability_is_explicit_when_dependency_missing(tmp_path) -> None:
    importer = StubImporter(fail=True)
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=importer,
    )

    capability = adapter.capability(enabled=True)

    assert capability == SemanticFilterCapability(
        available=False,
        reason="semantic filter unavailable: install careerkit[semantic]",
    )
    assert importer.calls == ["sentence_transformers"]
    assert not (tmp_path / "private" / ".cache").exists()


def test_semantic_filter_disabled_skips_import(tmp_path) -> None:
    importer = StubImporter(fail=True)
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=importer,
    )

    capability = adapter.capability(enabled=False)

    assert capability.available is True
    assert capability.reason is None
    assert importer.calls == []


def test_semantic_filter_model_load_failure_disables_filter_with_diagnostic(tmp_path, monkeypatch) -> None:
    sentence_transformers = ModuleType("sentence_transformers")
    setattr(sentence_transformers, "SentenceTransformer", BrokenModel)
    fake_numpy = FakeNumpy()
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    def importer(name: str):
        if name == "sentence_transformers":
            return sentence_transformers
        return fake_numpy

    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=importer,
    )

    assert adapter.classify("Product Manager") is None
    assert adapter.diagnostic == "semantic filter unavailable: model unavailable offline"
    assert adapter.capability(enabled=True) == SemanticFilterCapability(
        available=False,
        reason="semantic filter unavailable: model unavailable offline",
    )


def test_semantic_filter_classify_preserves_characterized_title_decisions(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=_semantic_importer(
            {
                "Product Manager": [0.0, 1.0],
                "Frontend Developer": [0.0, 1.0],
            }
        ),
    )

    assert adapter.classify("Backend Engineer") is None
    assert adapter.classify("[Remote] Product Manager") == "pass"
    assert adapter.score_title("[Remote]  Product   Manager").normalized_title == "[remote] product manager"
    assert adapter.classify("Frontend Developer") == "pass"
    assert adapter.classify("") is None


def test_semantic_filter_local_and_custom_models_keep_current_fail_open_decisions(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    local_model_root = tmp_path / "models" / "local"
    local_model_root.mkdir(parents=True)
    (local_model_root / "config.json").write_text('{"ok": true}', encoding="utf-8")
    scores = {"Product Manager": [0.0, 1.0]}

    for model_name in (str(local_model_root), "acme/custom-backend-filter"):
        adapter = SemanticFilterAdapter(
            workspace=WorkspacePaths(root=tmp_path, source="explicit"),
            importer=_semantic_importer(scores),
            model_name=model_name,
        )

        assert adapter.classify("Product Manager") == "pass"


def test_semantic_filter_known_hub_models_pin_approved_revisions_and_emit_provenance(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    cases = (
        (
            "jhgan/ko-sroberta-multitask",
            "8fca7c9c98c26599be0e14b9916b11a756a26f19",
        ),
        (
            "all-MiniLM-L6-v2",
            "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        ),
    )

    for model_name, expected_revision in cases:
        modules = _semantic_modules({"Product Manager": [0.0, 1.0]})
        adapter = SemanticFilterAdapter(
            workspace=WorkspacePaths(root=tmp_path, source="explicit"),
            importer=modules.__getitem__,
            model_name=model_name,
        )

        adapter.prepare()

        assert adapter.classify("Product Manager") == "pass"
        assert modules["sentence_transformers"].SentenceTransformer.init_calls == [
            (model_name, expected_revision)
        ]
        provenance = adapter.provenance()
        assert provenance.model_name == model_name
        assert provenance.model_revision == expected_revision
        assert provenance.sentence_transformers_version == "3.0.1"
        assert provenance.anchor_digest
        assert provenance.keyword_override_digest
        adapter.close()


def test_semantic_filter_default_model_provenance_and_cache_key_inputs_are_exact(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    modules = _semantic_modules({"Product Manager": [0.0, 1.0]}, version="5.1.0")
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=modules.__getitem__,
    )

    adapter.prepare()

    cache_files = sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))
    assert len(cache_files) == 1
    provenance = adapter.provenance()
    assert adapter.model_name == DEFAULT_MODEL
    assert adapter.threshold == MODEL_THRESHOLDS[DEFAULT_MODEL]
    assert provenance.model_revision == APPROVED_MODEL_REVISIONS[DEFAULT_MODEL]
    assert provenance.sentence_transformers_version == "5.1.0"
    assert provenance.anchor_digest == EXPECTED_DEFAULT_ANCHOR_DIGEST
    assert provenance.keyword_override_digest == KEYWORD_OVERRIDE_DIGEST

    expected_cache_path = tmp_path / "private/.cache" / f"semantic_centroids_{EXPECTED_DEFAULT_CACHE_KEY}.pkl"
    assert cache_files == [expected_cache_path]


def test_semantic_filter_custom_revision_and_unpinned_custom_hub_provenance(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    pinned_revision = "1234567890abcdef1234567890abcdef12345678"
    pinned_modules = _semantic_modules({"Product Manager": [0.0, 1.0]})
    pinned = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=pinned_modules.__getitem__,
        model_name="acme/custom-backend-filter",
        model_revision=pinned_revision,
    )

    pinned.prepare()

    assert pinned.classify("Product Manager") == "pass"
    assert pinned.provenance().model_revision == pinned_revision
    assert pinned_modules["sentence_transformers"].SentenceTransformer.init_calls == [
        ("acme/custom-backend-filter", pinned_revision)
    ]

    unpinned_modules = _semantic_modules({"Product Manager": [0.0, 1.0]})
    unpinned = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=unpinned_modules.__getitem__,
        model_name="acme/custom-backend-filter",
    )

    unpinned.prepare()

    assert unpinned.classify("Product Manager") == "pass"
    assert unpinned_modules["sentence_transformers"].SentenceTransformer.init_calls == [
        ("acme/custom-backend-filter", None)
    ]
    try:
        unpinned.provenance()
    except ValueError as exc:
        assert str(exc) == "semantic filter unavailable: model revision required for custom hub models"
    else:
        raise AssertionError("expected unpinned custom hub provenance to be unavailable")


def test_semantic_filter_local_model_digest_and_cache_key_inputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    local_model_root = tmp_path / "models" / "local"
    local_model_root.mkdir(parents=True)
    (local_model_root / "config.json").write_text('{"name":"local"}', encoding="utf-8")
    (local_model_root / "weights.bin").write_bytes(b"v1")
    scores = {"Product Manager": [0.0, 1.0]}

    default_modules = _semantic_modules(scores)
    default_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=default_modules.__getitem__,
    )
    default_adapter.prepare()
    default_cache = sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))
    assert len(default_cache) == 1

    same_threshold_modules = _semantic_modules(scores)
    same_threshold_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=same_threshold_modules.__getitem__,
        threshold=-0.25,
    )
    same_threshold_adapter.prepare()
    assert sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl")) == default_cache

    custom_modules = _semantic_modules(scores)
    custom_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=custom_modules.__getitem__,
        model_name="acme/custom-backend-filter",
        model_revision="1234567890abcdef1234567890abcdef12345678",
    )
    custom_adapter.prepare()

    local_modules = _semantic_modules(scores)
    local_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=local_modules.__getitem__,
        model_name=str(local_model_root),
    )
    local_adapter.prepare()

    initial_provenance = local_adapter.provenance()
    assert initial_provenance.model_revision.startswith("sha256:")
    cache_after_local = sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))
    assert len(cache_after_local) == 3

    local_modules_v2 = _semantic_modules(scores)
    local_adapter_v2 = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=local_modules_v2.__getitem__,
        model_name=str(local_model_root),
    )
    (local_model_root / "weights.bin").write_bytes(b"v2")
    local_adapter_v2.prepare()
    mutated_provenance = local_adapter_v2.provenance()
    assert mutated_provenance.model_revision != initial_provenance.model_revision
    assert len(sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))) == 4


def test_semantic_filter_unavailable_provenance_covers_symlinks_and_local_mutation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    local_model_root = tmp_path / "models" / "local"
    local_model_root.mkdir(parents=True)
    (local_model_root / "config.json").write_text('{"name":"local"}', encoding="utf-8")
    (local_model_root / "weights.bin").write_bytes(b"v1")
    scores = {"Product Manager": [0.0, 1.0]}

    symlink_root = tmp_path / "models" / "symlinked"
    symlink_root.mkdir(parents=True)
    (symlink_root / "config.json").write_text('{"name":"local"}', encoding="utf-8")
    (symlink_root / "linked.bin").symlink_to(local_model_root / "weights.bin")
    symlink_modules = _semantic_modules(scores)
    symlink_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=symlink_modules.__getitem__,
        model_name=str(symlink_root),
    )

    symlink_adapter.prepare()

    try:
        symlink_adapter.provenance()
    except ValueError as exc:
        assert str(exc) == "semantic filter unavailable: symlinked local model content is not allowed"
    else:
        raise AssertionError("expected symlinked local model provenance to be unavailable")

    mutation_modules = _semantic_modules(scores)
    mutation_adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=mutation_modules.__getitem__,
        model_name=str(local_model_root),
    )
    mutation_adapter.prepare()
    (local_model_root / "weights.bin").write_bytes(b"mutated-during-runtime")

    assert mutation_adapter.classify("Product Manager") is None
    assert mutation_adapter.diagnostic == "semantic filter unavailable: local model content changed during scoring"


def test_semantic_filter_close_clears_transient_failure_for_reuse(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    broken_sentence_transformers = ModuleType("sentence_transformers")
    setattr(broken_sentence_transformers, "SentenceTransformer", BrokenModel)

    def broken_importer(name: str):
        if name == "sentence_transformers":
            return broken_sentence_transformers
        return FakeNumpy()

    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=broken_importer,
    )

    assert adapter.classify("Product Manager") is None
    assert adapter.diagnostic == "semantic filter unavailable: model unavailable offline"

    adapter.close()
    adapter.importer = _semantic_importer({"Product Manager": [0.0, 1.0]})

    assert adapter.classify("Product Manager") == "pass"
    assert adapter.diagnostic is None


def test_semantic_filter_close_reloads_centroids_after_local_model_mutation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())
    local_model_root = tmp_path / "models" / "local"
    local_model_root.mkdir(parents=True)
    (local_model_root / "config.json").write_text('{"name":"local"}', encoding="utf-8")
    weights = local_model_root / "weights.bin"
    weights.write_bytes(b"v1")
    importer = MutableImporter(
        _semantic_modules(
            {"Payroll Specialist": [0.0, 1.0]},
            backend_anchor_vector=[1.0, 0.0],
            non_backend_anchor_vector=[0.0, 1.0],
        )
    )
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=importer,
        model_name=str(local_model_root),
    )

    assert adapter.classify("Payroll Specialist") == "pass"
    initial_provenance = adapter.provenance()
    initial_cache_files = sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))
    assert len(initial_cache_files) == 1

    adapter.close()
    weights.write_bytes(b"v2")
    importer.modules = _semantic_modules(
        {"Payroll Specialist": [0.0, 1.0]},
        backend_anchor_vector=[0.0, 1.0],
        non_backend_anchor_vector=[1.0, 0.0],
    )

    assert adapter.classify("Payroll Specialist") is None
    mutated_provenance = adapter.provenance()
    assert mutated_provenance.model_revision != initial_provenance.model_revision
    assert len(sorted(tmp_path.glob("private/.cache/semantic_centroids_*.pkl"))) == 2
