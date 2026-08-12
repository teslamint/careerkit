from __future__ import annotations

import hashlib
import importlib
import logging
import os
import pickle
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, cast

from careerkit.jobs.application.semantic_eval import (
    SCORE_CONTRACT_DIGEST,
    SemanticModelProvenance,
    SemanticTitleScore,
    normalize_eval_title,
)
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
APPROVED_MODEL_REVISIONS = {
    DEFAULT_MODEL: "8fca7c9c98c26599be0e14b9916b11a756a26f19",
    "all-MiniLM-L6-v2": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
}
UNAVAILABLE_MESSAGE = "semantic filter unavailable: install careerkit[semantic]"
RUNTIME_UNAVAILABLE_PREFIX = "semantic filter unavailable"
CUSTOM_REVISION_REQUIRED = "model revision required for custom hub models"
LOCAL_SYMLINK_MESSAGE = "symlinked local model content is not allowed"
LOCAL_MUTATION_MESSAGE = "local model content changed during scoring"
UNKNOWN_GIT_SHA = "unknown"
PROVENANCE_COMMAND = "career-jobs semantic-eval run"
PENDING_DATASET_DIGEST = "pending-dataset"
PENDING_SPLIT_DIGEST = "pending-split"
PENDING_FAMILY_LOCK_DIGEST = "pending-family-lock"
KEYWORD_OVERRIDE_DIGEST = hashlib.sha256(b"backend-keyword-override/v1").hexdigest()


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
    model_revision: str | None = None

    _model: EmbeddingModel | None = None
    _backend_centroid: Any = None
    _non_backend_centroid: Any = None
    _ready: bool = False
    _failure_reason: str | None = None
    _provenance_unavailable_reason: str | None = None
    _resolved_model_revision: str | None = None
    _sentence_transformers_version: str | None = None
    _local_model_root: Path | None = None
    _local_model_fingerprint: str | None = None

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

    def prepare(self) -> None:
        self._ensure_loaded()

    def score_title(self, title: str) -> SemanticTitleScore:
        cleaned = strip_bracket_prefix(title)
        normalized = normalize_eval_title(title)
        if self._failure_reason is not None or not cleaned or has_backend_keyword(cleaned):
            return self._build_score(title, normalized, 0.0, 0.0)
        try:
            if not self._ensure_loaded():
                return self._build_score(title, normalized, 0.0, 0.0)
            if self._local_model_root is not None and self._local_model_fingerprint is not None:
                current_fingerprint = self._fingerprint_local_model(self._local_model_root)
                if current_fingerprint != self._local_model_fingerprint:
                    raise ValueError(LOCAL_MUTATION_MESSAGE)
            np = cast(Any, self.importer("numpy"))
            model = cast(EmbeddingModel, self._model)
            backend_centroid = self._backend_centroid
            non_backend_centroid = self._non_backend_centroid
            embedding = model.encode([cleaned], normalize_embeddings=True)[0]
            backend_score = float(np.dot(embedding, backend_centroid))
            non_backend_score = float(np.dot(embedding, non_backend_centroid))
            return self._build_score(title, normalized, backend_score, non_backend_score)
        except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._disable(exc)
            return self._build_score(title, normalized, 0.0, 0.0)

    def provenance(self) -> SemanticModelProvenance:
        if not self._ensure_loaded():
            raise ValueError(self._failure_reason or UNAVAILABLE_MESSAGE)
        if self._provenance_unavailable_reason is not None:
            raise ValueError(f"{RUNTIME_UNAVAILABLE_PREFIX}: {self._provenance_unavailable_reason}")
        assert self._resolved_model_revision is not None
        assert self._sentence_transformers_version is not None
        return SemanticModelProvenance(
            model_name=self.model_name,
            model_revision=self._resolved_model_revision,
            sentence_transformers_version=self._sentence_transformers_version,
            anchor_digest=self._anchor_digest(),
            keyword_override_digest=KEYWORD_OVERRIDE_DIGEST,
            dataset_digest=PENDING_DATASET_DIGEST,
            split_digest=PENDING_SPLIT_DIGEST,
            family_lock_digest=PENDING_FAMILY_LOCK_DIGEST,
            git_sha=UNKNOWN_GIT_SHA,
            command=PROVENANCE_COMMAND,
            score_contract_digest=SCORE_CONTRACT_DIGEST,
        )

    def close(self) -> None:
        self._model = None
        self._backend_centroid = None
        self._non_backend_centroid = None
        self._ready = False
        self._failure_reason = None
        self._provenance_unavailable_reason = None
        self._resolved_model_revision = None
        self._sentence_transformers_version = None
        self._local_model_root = None
        self._local_model_fingerprint = None

    def classify(self, title: str) -> str | None:
        score = self.score_title(title)
        return "pass" if score.reject else None

    @property
    def diagnostic(self) -> str | None:
        return self._failure_reason

    def _build_score(
        self,
        title: str,
        normalized_title: str,
        backend_score: float,
        non_backend_score: float,
    ) -> SemanticTitleScore:
        relative_score = backend_score - non_backend_score
        return SemanticTitleScore(
            title=title,
            normalized_title=normalized_title,
            backend_score=backend_score,
            non_backend_score=non_backend_score,
            relative_score=relative_score,
            reject=bool(normalized_title and relative_score < self.threshold),
        )

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
            np = cast(Any, self.importer("numpy"))
        except ImportError:
            logger.warning(UNAVAILABLE_MESSAGE)
            return False
        model_cls = getattr(sentence_transformers, "SentenceTransformer")
        local_model_root = self._local_model_path()
        fingerprint_for_cache = self._resolve_model_fingerprint(local_model_root)
        resolved_revision = self._resolve_model_revision(local_model_root, fingerprint_for_cache)
        cache_path = self._cache_path(
            fingerprint=fingerprint_for_cache,
            sentence_transformers_version=str(getattr(sentence_transformers, "__version__", "unknown")),
        )
        if cache_path.exists():
            try:
                with cache_path.open("rb") as handle:
                    data = pickle.load(handle)
                self._backend_centroid = data["backend"]
                self._non_backend_centroid = data["non_backend"]
            except (pickle.UnpicklingError, KeyError, EOFError):
                cache_path.unlink(missing_ok=True)
        try:
            model_kwargs = {}
            if resolved_revision is not None and local_model_root is None:
                model_kwargs["revision"] = resolved_revision
            self._model = cast(EmbeddingModel, model_cls(self.model_name, **model_kwargs))
            if local_model_root is not None and fingerprint_for_cache.startswith("sha256:"):
                verified_fingerprint = self._fingerprint_local_model(local_model_root)
                if verified_fingerprint != fingerprint_for_cache:
                    raise ValueError(LOCAL_MUTATION_MESSAGE)
            if self._backend_centroid is None:
                model = cast(EmbeddingModel, self._model)
                backend_embeddings = model.encode(BACKEND_ANCHORS, normalize_embeddings=True)
                non_backend_embeddings = model.encode(NON_BACKEND_ANCHORS, normalize_embeddings=True)
                backend_centroid = np.mean(backend_embeddings, axis=0)
                backend_centroid /= np.linalg.norm(backend_centroid)
                non_backend_centroid = np.mean(non_backend_embeddings, axis=0)
                non_backend_centroid /= np.linalg.norm(non_backend_centroid)
                self._backend_centroid = backend_centroid
                self._non_backend_centroid = non_backend_centroid
                self._publish_cache(cache_path, {"backend": backend_centroid, "non_backend": non_backend_centroid})
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._disable(exc)
            return False
        self._local_model_root = local_model_root
        self._local_model_fingerprint = fingerprint_for_cache if fingerprint_for_cache.startswith("sha256:") else None
        self._resolved_model_revision = resolved_revision or fingerprint_for_cache
        self._sentence_transformers_version = str(getattr(sentence_transformers, "__version__", "unknown"))
        self._ready = True
        return True

    def _resolve_model_fingerprint(self, local_model_root: Path | None) -> str:
        self._provenance_unavailable_reason = None
        if local_model_root is None:
            return self.model_revision or APPROVED_MODEL_REVISIONS.get(self.model_name, self.model_name)
        try:
            return self._fingerprint_local_model(local_model_root)
        except ValueError as exc:
            self._provenance_unavailable_reason = str(exc)
            return f"unavailable:{local_model_root}"

    def _resolve_model_revision(
        self,
        local_model_root: Path | None,
        fingerprint: str,
    ) -> str | None:
        if local_model_root is not None:
            return fingerprint if fingerprint.startswith("sha256:") else None
        if self.model_revision:
            return self.model_revision
        approved_revision = APPROVED_MODEL_REVISIONS.get(self.model_name)
        if approved_revision is not None:
            return approved_revision
        self._provenance_unavailable_reason = CUSTOM_REVISION_REQUIRED
        return None

    def _local_model_path(self) -> Path | None:
        candidate = Path(self.model_name).expanduser()
        if candidate.exists():
            return candidate
        return None

    def _cache_path(self, *, fingerprint: str, sentence_transformers_version: str) -> Path:
        material = "\0".join(
            [
                self.model_name,
                fingerprint,
                sentence_transformers_version,
                self._anchor_digest(),
            ]
        )
        cache_key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
        return self.workspace.cache_dir / f"semantic_centroids_{cache_key}.pkl"

    def _publish_cache(self, cache_path: Path, payload: dict[str, object]) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=cache_path.parent,
            prefix=f".{cache_path.name}.",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                pickle.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(cache_path)
        except OSError:
            temporary_path.unlink(missing_ok=True)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        else:
            temporary_path.unlink(missing_ok=True)

    def _fingerprint_local_model(self, root: Path) -> str:
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError(LOCAL_SYMLINK_MESSAGE)
            if path.is_file():
                files.append(path)
        digest = hashlib.sha256()
        for path in files:
            relative_path = path.relative_to(root).as_posix()
            stat = path.stat()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_dev).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_ino).encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(stat.st_size).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _anchor_digest(self) -> str:
        material = "\0".join([*BACKEND_ANCHORS, "", *NON_BACKEND_ANCHORS])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
