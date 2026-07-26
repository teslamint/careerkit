from __future__ import annotations

from types import ModuleType

from careerkit.jobs.adapters.semantic_filter import SemanticFilterAdapter, SemanticFilterCapability
from careerkit.workspace import WorkspacePaths


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
    def __init__(self, _: str) -> None:
        raise OSError("model unavailable offline")


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


def test_semantic_filter_model_load_failure_disables_filter_with_diagnostic(tmp_path) -> None:
    sentence_transformers = ModuleType("sentence_transformers")
    setattr(sentence_transformers, "SentenceTransformer", BrokenModel)
    numpy = ModuleType("numpy")
    modules = {"sentence_transformers": sentence_transformers, "numpy": numpy}
    adapter = SemanticFilterAdapter(
        workspace=WorkspacePaths(root=tmp_path, source="explicit"),
        importer=modules.__getitem__,
    )

    assert adapter.classify("Product Manager") is None
    assert adapter.diagnostic == "semantic filter unavailable: model unavailable offline"
    assert adapter.capability(enabled=True) == SemanticFilterCapability(
        available=False,
        reason="semantic filter unavailable: model unavailable offline",
    )
