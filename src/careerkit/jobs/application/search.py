from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Any, Literal, Mapping, Protocol, TypeAlias, cast

from careerkit.jobs.application.semantic_eval import SemanticCaptureSink
from careerkit.jobs.application.title_filter import normalize_job_queries, quick_filter_title
from careerkit.jobs.domain.filters import is_rejected_company_name

_UNREALISTIC_MAX = 50

StopReason: TypeAlias = Literal[
    "api_end",
    "repeated_page",
    "no_new_items",
    "malformed_response",
    "malformed_page",
    "safety_page_limit",
    "safety_time_limit",
    "request_error",
]
_STOP_REASON_VALUES = frozenset(
    {
        "api_end",
        "repeated_page",
        "no_new_items",
        "malformed_response",
        "malformed_page",
        "safety_page_limit",
        "safety_time_limit",
        "request_error",
    }
)


def _normalize_stop_reason(value: str | None) -> StopReason | None:
    if value not in _STOP_REASON_VALUES:
        return None
    return cast(StopReason, value)


@dataclass(frozen=True)
class SearchCandidate:
    platform: str
    job_id: str
    raw_id: str
    title: str
    company: str
    experience: str
    url: str

    @property
    def seen_key(self) -> str:
        return f"{self.platform}:{self.job_id}"


@dataclass(frozen=True)
class PaginatedItems:
    items: tuple[SearchCandidate, ...] = ()
    total_count: int | None = None
    pages_fetched: int = 0
    complete: bool = True
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class PlatformSearchBatch:
    candidates: list[SearchCandidate] = field(default_factory=list)
    total_count: int | None = None
    pages_fetched: int = 0
    complete: bool = True
    stop_reason: StopReason | None = None


@dataclass(frozen=True)
class SearchState:
    seen_job_keys: set[str]


@dataclass(frozen=True)
class SearchResult:
    postings: tuple[SearchCandidate, ...]
    updated_seen_job_keys: set[str]
    diagnostics: tuple[str, ...] = ()
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_found: int = 0
    filtered_out: int = 0
    duplicates: int = 0
    persisted_duplicates: int = 0
    run_duplicates: int = 0
    filesystem_duplicates: int = 0


class SemanticFilter(Protocol):
    def classify(self, title: str) -> str | None: ...


class SearchAdapter(Protocol):
    @property
    def supports_search(self) -> bool: ...

    def search(self, query: str, *, config, state) -> PlatformSearchBatch | PaginatedItems: ...


def page_fingerprint(items: list[dict]) -> tuple[str, ...]:
    return tuple(str(item.get("id", item)) for item in items)


def parse_experience_range(exp_str: str | None) -> tuple[int | None, int | None]:
    if not exp_str:
        return None, None
    exp_str = re.sub(r"\*+", "", exp_str).strip()
    if re.search(r"경력\s*무관|무관", exp_str):
        return None, None
    if re.fullmatch(r"\s*경력\s*", exp_str):
        return None, None
    compound = re.search(r"(\d+)\s*년\s*이상\s*~?\s*(\d+)\s*년\s*(?:이하|미만)", exp_str)
    if compound:
        min_y, max_y = int(compound.group(1)), int(compound.group(2))
        if "미만" in exp_str:
            max_y -= 1
        return (min_y, None) if max_y >= _UNREALISTIC_MAX else (min_y, max_y)
    range_match = re.search(r"(\d+)\s*년?\s*[-~]\s*(\d+)\s*년(?:\s*차)?", exp_str)
    if range_match:
        min_y, max_y = int(range_match.group(1)), int(range_match.group(2))
        return (min_y, None) if max_y >= _UNREALISTIC_MAX else (min_y, max_y)
    min_match = re.search(r"(\d+)\s*년\s*(?:차\s*)?(?:↑|이상|\+)", exp_str)
    if min_match:
        return int(min_match.group(1)), None
    max_match = re.fullmatch(r"\s*(?:경력\s*)?(\d+)\s*년(?:\s*차)?\s*(?:이하|미만)\s*", exp_str)
    if max_match:
        max_y = int(max_match.group(1))
        if "미만" in exp_str:
            max_y -= 1
        return None, max_y
    exact_match = re.fullmatch(r"\s*(?:경력\s*)?(\d+)\s*년(?:\s*차)?\s*", exp_str)
    if exact_match:
        years = int(exact_match.group(1))
        return years, years
    return None, None


def filter_experience(exp_str: str | None, config: Mapping[str, Any], *, min_years: int | None = None, max_years: int | None = None) -> bool:
    filters = dict(config.get("filters", {})) if isinstance(config.get("filters", {}), Mapping) else {}
    min_upper = filters.get("min_experience_upper", 14)
    max_exp_cfg = filters.get("max_experience")
    if min_years is None and max_years is None:
        min_years, max_years = parse_experience_range(exp_str)
    if max_years is not None and max_years < min_upper:
        return True
    if max_exp_cfg is not None and min_years is not None and min_years > max_exp_cfg:
        return True
    return False


def _quick_filter_config_digest(quick_filters: Mapping[str, Any]) -> str:
    material = json.dumps(
        _canonicalize_quick_filter_value(dict(quick_filters)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonicalize_quick_filter_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize_quick_filter_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple):
        return [_canonicalize_quick_filter_value(item) for item in value]
    if isinstance(value, list):
        return [_canonicalize_quick_filter_value(item) for item in value]
    return value


class SearchService:
    def __init__(
        self,
        *,
        adapters: Mapping[str, SearchAdapter],
        semantic_filter: SemanticFilter | None,
        semantic_capability: dict[str, Any] | None = None,
        existing_record_checker=None,
        semantic_capture_sink: SemanticCaptureSink | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.semantic_filter = semantic_filter
        self.semantic_capability = semantic_capability or {"available": True, "reason": None}
        self.existing_record_checker = existing_record_checker or (lambda platform, raw_id: False)
        self.semantic_capture_sink = semantic_capture_sink

    def run(self, config, state: SearchState) -> SearchResult:
        queries = normalize_job_queries(list(config.search_queries))
        quick_filter_digest = _quick_filter_config_digest(config.quick_filters)
        diagnostics: list[str] = []
        semantic_capability = dict(self.semantic_capability)
        if config.semantic_enabled and not semantic_capability.get("available", True):
            reason = semantic_capability.get("reason")
            if reason:
                diagnostics.append(reason)
        total_found = filtered_out = duplicates = persisted_duplicates = run_duplicates = filesystem_duplicates = 0
        combined_seen = set(state.seen_job_keys)
        candidate_keys: set[str] = set()
        canonical_duplicate_keys: set[str] = set()
        unique_candidates: list[SearchCandidate] = []
        for platform_name, platform_config in config.platforms.items():
            if not platform_config.enabled:
                continue
            adapter = self.adapters.get(platform_name)
            if adapter is None or not adapter.supports_search:
                continue
            platform_queries = queries[:1] if getattr(adapter, "query_independent", False) else queries
            for query in platform_queries:
                try:
                    batch = adapter.search(query, config=config, state=state)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                    if self.semantic_capture_sink is not None:
                        self.semantic_capture_sink.record_source_outcome(
                            platform_name,
                            complete=False,
                            stop_reason='request_error',
                            pages_fetched=0,
                        )
                        self.semantic_capture_sink.mark_incomplete('platform_failure')
                    diagnostics.append(f"{platform_name} search failed: {exc}")
                    break
                request_delay = float(config.execution.get("request_delay", 0.0))
                if request_delay > 0:
                    time.sleep(request_delay)
                if self.semantic_capture_sink is not None:
                    self.semantic_capture_sink.record_source_outcome(
                        platform_name,
                        complete=batch.complete,
                        stop_reason=batch.stop_reason,
                        pages_fetched=batch.pages_fetched,
                    )
                if not batch.complete:
                    if self.semantic_capture_sink is not None:
                        self.semantic_capture_sink.mark_incomplete('search_incomplete')
                    message = f"{platform_name} search incomplete after {batch.pages_fetched} pages"
                    stop_reason = _normalize_stop_reason(batch.stop_reason)
                    if stop_reason:
                        message += f": {stop_reason}"
                    diagnostics.append(message)
                items = list(batch.items if isinstance(batch, PaginatedItems) else batch.candidates)
                for candidate in items:
                    total_found += 1
                    qf = quick_filter_title(candidate.title, {"quick_filters": config.quick_filters})
                    if qf == "pass":
                        filtered_out += 1
                        continue
                    if self.semantic_filter is not None:
                        if self.semantic_capture_sink is not None:
                            self.semantic_capture_sink.capture(
                                candidate.title,
                                quick_filter_outcome='eligible',
                                quick_filter_config_digest=quick_filter_digest,
                            )
                        classification = self.semantic_filter.classify(candidate.title)
                        runtime_diagnostic = getattr(self.semantic_filter, "diagnostic", None)
                        if runtime_diagnostic:
                            if runtime_diagnostic not in diagnostics:
                                diagnostics.append(runtime_diagnostic)
                            semantic_capability = {
                                "available": False,
                                "reason": runtime_diagnostic,
                            }
                            if self.semantic_capture_sink is not None:
                                self.semantic_capture_sink.mark_incomplete('semantic_unavailable')
                        if classification == "pass":
                            filtered_out += 1
                            continue
                    if is_rejected_company_name(candidate.company, config.rejected_companies, list(config.quick_filters.get("company_exclude", []))):
                        filtered_out += 1
                        continue
                    if filter_experience(candidate.experience, {"filters": config.filters}):
                        filtered_out += 1
                        continue
                    if candidate.seen_key in combined_seen:
                        duplicates += 1
                        persisted_duplicates += 1
                        continue
                    if self.existing_record_checker(candidate.platform, candidate.raw_id):
                        duplicates += 1
                        filesystem_duplicates += 1
                        canonical_duplicate_keys.add(candidate.seen_key)
                        continue
                    if candidate.seen_key in candidate_keys:
                        duplicates += 1
                        run_duplicates += 1
                        continue
                    candidate_keys.add(candidate.seen_key)
                    unique_candidates.append(candidate)
        final_postings = tuple(unique_candidates[: config.max_urls_per_run])
        updated_seen = {
            candidate.seen_key for candidate in final_postings
        } | canonical_duplicate_keys
        return SearchResult(
            postings=final_postings,
            updated_seen_job_keys=updated_seen,
            diagnostics=tuple(diagnostics),
            capabilities={"semantic_filter": semantic_capability},
            total_found=total_found,
            filtered_out=filtered_out,
            duplicates=duplicates,
            persisted_duplicates=persisted_duplicates,
            run_duplicates=run_duplicates,
            filesystem_duplicates=filesystem_duplicates,
        )
