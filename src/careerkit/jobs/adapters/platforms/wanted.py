from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlencode

from careerkit.jobs.adapters.http import HttpClient
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, page_fingerprint

WANTED_API_BASE = "https://www.wanted.co.kr/api/v4"
WANTED_BASE_URL = "https://www.wanted.co.kr"
WANTED_HEADERS = {"Accept": "application/json", "Referer": f"{WANTED_BASE_URL}/"}
WANTED_BACKEND_MAPPING = {"job_group_id": 518, "job_ids": [872]}
_DEFAULT_LIMIT = 20
_MAX_PAGES = 1000


@dataclass(frozen=True)
class WantedAdapter:
    name: str = "wanted"
    supports_search: bool = True
    query_independent: bool = True

    def native_role_mapping(self) -> dict[str, object]:
        return dict(WANTED_BACKEND_MAPPING)

    def search(self, query: str, *, config, state, http: HttpClient | None = None) -> PaginatedItems:
        del query, state
        http_client = http or config.http_client
        platform = config.platforms[self.name]
        years = min(int(config.filters.get("api_min_experience", 10)), 10)
        all_items: list[dict] = []
        seen_pages: set[tuple[str, ...]] = set()
        offset = 0
        pages_fetched = 0
        while True:
            if pages_fetched >= _MAX_PAGES:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), complete=False, pages_fetched=pages_fetched)
            params = {
                "country": "kr",
                "job_sort": "job.latest_order",
                "locations": "all",
                "years": years,
                "tag_type_ids": WANTED_BACKEND_MAPPING["job_ids"],
                "limit": _DEFAULT_LIMIT,
                "offset": offset,
            }
            try:
                data = http_client.request_json(
                    f"{WANTED_API_BASE}/jobs?{urlencode(params, doseq=True)}",
                    headers=WANTED_HEADERS,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                if not all_items:
                    raise
                return PaginatedItems(
                    items=tuple(
                        self._to_candidate(item, platform.base_url)
                        for item in all_items
                    ),
                    complete=False,
                    pages_fetched=pages_fetched,
                )
            items = data.get("data", [])
            pages_fetched += 1
            if not items:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), pages_fetched=pages_fetched)
            fingerprint = page_fingerprint(items)
            if fingerprint in seen_pages:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), complete=False, pages_fetched=pages_fetched)
            seen_pages.add(fingerprint)
            all_items.extend(items)
            if not data.get("links", {}).get("next"):
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), pages_fetched=pages_fetched)
            next_offset = offset + len(items)
            if next_offset <= offset:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), complete=False, pages_fetched=pages_fetched)
            offset = next_offset
            delay = float(config.rate_limits.get(self.name, 0.0))
            if delay > 0:
                time.sleep(delay)

    def _to_candidate(self, item: dict, base_url: str) -> SearchCandidate:
        item_id = str(item.get("id"))
        company = item.get("company") or {}
        annual_from = item.get("annual_from")
        annual_to = item.get("annual_to")
        experience = ""
        if annual_from is not None and annual_to is not None:
            if annual_from == 0 and annual_to == 0:
                experience = "신입"
            elif annual_from == 0:
                experience = f"신입~{annual_to}년"
            else:
                experience = f"{annual_from}~{annual_to}년"
        elif annual_from:
            experience = f"{annual_from}년 이상"
        return SearchCandidate(platform=self.name, job_id=item_id, raw_id=item_id, title=(item.get("position") or "").strip(), company=(company.get("name") or "").strip(), experience=experience, url=f"{base_url}/wd/{item_id}")
