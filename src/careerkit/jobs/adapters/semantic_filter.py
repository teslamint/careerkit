from __future__ import annotations

import hashlib
import importlib
import logging
import pickle
from dataclasses import dataclass
from typing import Any, Callable, Protocol, cast

from careerkit.jobs.application.title_filter import has_backend_keyword, strip_bracket_prefix
from careerkit.workspace import WorkspacePaths

logger = logging.getLogger(__name__)

BACKEND_ANCHORS = [
    "백엔드 엔지니어", "백엔드 개발자", "Backend Engineer", "Backend Developer", "서버 개발자", "Server Developer", "Server Engineer",
]
NON_BACKEND_ANCHORS = [
    "AI Engineer", "ML Engineer", "프론트엔드 개발자", "Frontend Developer", "iOS Developer", "Android 개발자", "DevOps Engineer", "SRE Engineer", "Data Engineer", "데이터 엔지니어", "QA Engineer", "Product Manager", "System Engineer", "시스템 엔지니어", "Cloud Engineer", "인프라 엔지니어", "Embedded Engineer", "Hardware Engineer", "Firmware Engineer", "기계 엔지니어",
]
DEFAULT_MODEL = "jhgan/ko-sroberta-multitask"
MODEL_THRESHOLDS = {DEFAULT_MODEL: -0.04, "all-MiniLM-L6-v2": -0.05}
UNAVAILABLE_MESSAGE = "semantic filter unavailable: install careerkit[semantic]"
RUNTIME_UNAVAILABLE_PREFIX = "semantic filter unavailable"


class EmbeddingModel(Protocol):
    def encode(self, inputs: list[str], *, normalize_embeddings: bool) -> Any: ...


@dataclass(frozen=True)
class SemanticFilterCapability:
    available: bool
    reason: str | None = None


@dataclass
class SemanticFilterAdapter:
    workspace: WorkspacePaths
    importer: Callable[[str], object] = importlib.import_module
    model_name: str = DEFAULT_MODEL
    threshold: float = MODEL_THRESHOLDS[DEFAULT_MODEL]

    _model: EmbeddingModel | None = None
    _backend_centroid: Any = None
    _non_backend_centroid: Any = None
    _ready: bool = False
    _failure_reason: str | None = None

    def capability(self, *, enabled: bool) -> SemanticFilterCapability:
        if not enabled:
            return SemanticFilterCapability(True, None)
        if self._failure_reason is not None:
            return SemanticFilterCapability(False, self._failure_reason)
        try:
            self.importer("sentence_transformers")
        except ImportError:
            return SemanticFilterCapability(False, UNAVAILABLE_MESSAGE)
        return SemanticFilterCapability(True, None)

    def classify(self, title: str) -> str | None:
        cleaned = strip_bracket_prefix(title)
        if self._failure_reason is not None or not cleaned or has_backend_keyword(cleaned):
            return None
        try:
            if not self._ensure_loaded():
                return None
            import numpy as np  # pyright: ignore[reportMissingImports] - optional semantic extra

            model = cast(EmbeddingModel, self._model)
            backend_centroid = self._backend_centroid
            non_backend_centroid = self._non_backend_centroid
            emb = model.encode([cleaned], normalize_embeddings=True)[0]
            score = float(np.dot(emb, backend_centroid) - np.dot(emb, non_backend_centroid))
            return "pass" if score < self.threshold else None
        except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._disable(exc)
            return None

    @property
    def diagnostic(self) -> str | None:
        return self._failure_reason

    def _disable(self, exc: Exception) -> None:
        detail = str(exc).strip() or type(exc).__name__
        self._failure_reason = f"{RUNTIME_UNAVAILABLE_PREFIX}: {detail}"
        self._ready = False
        self._model = None
        logger.warning(self._failure_reason)

    def _ensure_loaded(self) -> bool:
        if self._ready:
            return True
        try:
            sentence_transformers = self.importer("sentence_transformers")
            np = self.importer("numpy")
        except ImportError:
            logger.warning(UNAVAILABLE_MESSAGE)
            return False
        model_cls = getattr(sentence_transformers, "SentenceTransformer")
        cache_key = hashlib.md5(f"{self.model_name}:{BACKEND_ANCHORS}:{NON_BACKEND_ANCHORS}".encode()).hexdigest()[:12]
        cache_path = self.workspace.cache_dir / f"semantic_centroids_{cache_key}.pkl"
        if cache_path.exists():
            try:
                with cache_path.open("rb") as handle:
                    data = pickle.load(handle)
                self._backend_centroid = data["backend"]
                self._non_backend_centroid = data["non_backend"]
            except (pickle.UnpicklingError, KeyError, EOFError):
                cache_path.unlink(missing_ok=True)
        try:
            self._model = cast(EmbeddingModel, model_cls(self.model_name))
            np_module = cast(Any, np)
            if self._backend_centroid is None:
                model = cast(EmbeddingModel, self._model)
                backend_embeddings = model.encode(BACKEND_ANCHORS, normalize_embeddings=True)
                non_backend_embeddings = model.encode(NON_BACKEND_ANCHORS, normalize_embeddings=True)
                backend_centroid = np_module.mean(backend_embeddings, axis=0)
                backend_centroid /= np_module.linalg.norm(backend_centroid)
                non_backend_centroid = np_module.mean(non_backend_embeddings, axis=0)
                non_backend_centroid /= np_module.linalg.norm(non_backend_centroid)
                self._backend_centroid = backend_centroid
                self._non_backend_centroid = non_backend_centroid
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with cache_path.open("wb") as handle:
                        pickle.dump({"backend": backend_centroid, "non_backend": non_backend_centroid}, handle)
                except OSError:
                    pass
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._disable(exc)
            return False
        self._ready = True
        return True
