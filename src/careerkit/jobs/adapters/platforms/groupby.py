from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlencode

from careerkit.jobs.adapters.http import HttpClient
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, page_fingerprint

GROUPBY_API_BASE = "https://api.groupby.kr"
GROUPBY_BASE_URL = "https://groupby.kr"
GROUPBY_HEADERS = {"Accept": "application/json", "Origin": GROUPBY_BASE_URL}
GROUPBY_BACKEND_MAPPING = {"position_types": [2]}
_DEFAULT_LIMIT = 10
_MAX_PAGES = 1000


@dataclass(frozen=True)
class GroupByAdapter:
    name: str = "groupby"
    supports_search: bool = True
    query_independent: bool = True

    def native_role_mapping(self) -> dict[str, object]:
        return dict(GROUPBY_BACKEND_MAPPING)

    def search(self, query: str, *, config, state, http: HttpClient | None = None) -> PaginatedItems:
        del query, state
        http_client = http or config.http_client
        platform = config.platforms[self.name]
        min_experience = config.filters.get("api_min_experience")
        max_experience = config.filters.get("api_max_experience")
        all_items: list[dict] = []
        seen_pages: set[tuple[str, ...]] = set()
        offset = 0
        pages_fetched = 0
        total = 0
        while True:
            if pages_fetched >= _MAX_PAGES:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, complete=False, pages_fetched=pages_fetched)
            params = {
                "isAdvertising": "false",
                "limit": _DEFAULT_LIMIT,
                "offset": offset,
                "orderBy": "-updatedAt",
                "positionTypes": ",".join(
                    str(value) for value in GROUPBY_BACKEND_MAPPING["position_types"]
                ),
            }
            if min_experience not in (None, "") and max_experience not in (None, ""):
                params["minExperience"] = min_experience
                params["maxExperience"] = max_experience
            try:
                payload = http_client.request_json(f"{GROUPBY_API_BASE}/startup-positions?{urlencode(params, doseq=True)}", headers=GROUPBY_HEADERS)
            except (OSError, RuntimeError, ValueError):
                if not all_items:
                    raise
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, complete=False, pages_fetched=pages_fetched)
            envelope = payload.get("data")
            if payload.get("status") != 200 or not isinstance(envelope, dict):
                if all_items:
                    return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, complete=False, pages_fetched=pages_fetched)
                raise RuntimeError(
                    f"GroupBy API status {payload.get('status')}: "
                    f"{payload.get('msg', 'missing data')}"
                )
            data = envelope
            items = data.get("items", [])
            total = int(data.get("total", 0))
            pages_fetched += 1
            if not items:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, pages_fetched=pages_fetched)
            fingerprint = page_fingerprint(items)
            if fingerprint in seen_pages:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, complete=False, pages_fetched=pages_fetched)
            seen_pages.add(fingerprint)
            all_items.extend(items)
            if offset + len(items) >= total:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, pages_fetched=pages_fetched)
            next_offset = offset + len(items)
            if next_offset <= offset:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total, complete=False, pages_fetched=pages_fetched)
            offset = next_offset
            delay = float(config.rate_limits.get(self.name, 0.0))
            if delay > 0:
                time.sleep(delay)

    def _to_candidate(self, item: dict, base_url: str) -> SearchCandidate:
        raw_id = str(item.get("id"))
        company = item.get("company") or item.get("startup") or {}
        experience = format_groupby_experience(item)
        return SearchCandidate(platform=self.name, job_id=raw_id, raw_id=raw_id, title=(item.get("title") or item.get("name") or "").strip(), company=(company.get("name") or item.get("companyName") or "").strip(), experience=str(experience).strip(), url=f"{base_url}/positions/{raw_id}")


def format_groupby_experience(item: dict) -> str:
    career_type = str(item.get("careerType") or "").strip()
    if career_type in {"무관", "인턴"}:
        return f"경력 {career_type}"
    value = (
        item.get("experienceRange")
        or item.get("careerRange")
        or item.get("career_range")
    )
    if not value and (
        item.get("minCareerYear") not in (None, "")
        or item.get("maxCareerYear") not in (None, "")
    ):
        value = {
            "min": item.get("minCareerYear"),
            "max": item.get("maxCareerYear"),
        }
    if not isinstance(value, dict):
        return str(value or career_type).strip()
    minimum = value.get("min")
    maximum = value.get("max")
    if minimum in (None, "") and maximum in (None, ""):
        return f"경력 {career_type}" if career_type else ""
    if minimum == 0 and maximum in (0, None, ""):
        return "신입"
    if minimum not in (None, "") and maximum not in (None, ""):
        return f"{minimum}~{maximum}년"
    if minimum not in (None, ""):
        return f"{minimum}년 이상"
    return f"{maximum}년 이하"
