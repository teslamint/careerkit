from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, page_fingerprint

WANTED_API_BASE = "https://www.wanted.co.kr/api/v4"
WANTED_BASE_URL = "https://www.wanted.co.kr"
WANTED_HEADERS = {"Accept": "application/json", "Referer": f"{WANTED_BASE_URL}/"}
WANTED_BACKEND_MAPPING = {"job_group_id": 518, "job_ids": [872]}
_DEFAULT_LIMIT = 20
_MAX_PAGES = 1000


@dataclass(frozen=True)
class WantedCompanyInfo:
    company_id: int
    name: str
    industry: str
    founded_year: int | None
    location: str
    employee_count: int | None
    avg_salary_manwon: int | None
    hired_1y: int | None
    left_1y: int | None
    total_sales_eok: float | None
    sales_year: str
    tags: tuple[str, ...]
    description: str
    homepage: str


def _extract_wanted_next_data(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
    if not match:
        raise ValueError("missing __NEXT_DATA__ payload")
    return json.loads(match.group(1))


def _find_wanted_company_queries(
    data: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    queries = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydrateState", {})
        .get("queries", [])
    )
    info: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    for q in queries:
        if not isinstance(q, dict):
            continue
        qk = q.get("queryKey", [])
        state_data = q.get("state", {}).get("data")
        if not isinstance(state_data, dict):
            continue
        if any("companyInfo" in str(k) for k in qk):
            info = state_data
        elif any("companySummary" in str(k) for k in qk):
            summary = state_data
    if not info:
        raise ValueError("companyInfo query not found")
    return info, summary


def wanted_company_http(
    company_id: int | str,
    http: HttpClient | None = None,
) -> WantedCompanyInfo:
    client = http or UrllibHttpClient()
    url = f"{WANTED_BASE_URL}/company/{company_id}"
    html = client.request_text(url)
    data = _extract_wanted_next_data(html)
    info, summary = _find_wanted_company_queries(data)

    detail = summary.get("detail") or {}
    salary_data = summary.get("salary") or {}
    employee_data = summary.get("employee") or {}
    sales_data = summary.get("sales") or {}

    main_tags = [t.get("title", "") for t in (info.get("mainTags") or []) if isinstance(t, dict)]
    company_tags = [t.get("title", "") for t in (info.get("companyTags") or []) if isinstance(t, dict)]
    all_tags = [t for t in dict.fromkeys(main_tags + company_tags) if t]

    address = info.get("address") or {}
    full_location = address.get("full_location", "") if isinstance(address, dict) else ""
    if not full_location:
        full_location = info.get("location", "")

    salary_raw = salary_data.get("salary") if salary_data.get("salary") is not None else detail.get("salary")
    sales_raw = sales_data.get("total") if sales_data.get("total") is not None else detail.get("totalSales")
    sales_updated = sales_data.get("updatedAt", "")
    sales_year = sales_updated[:4] if sales_updated and len(sales_updated) >= 4 else ""

    return WantedCompanyInfo(
        company_id=int(company_id),
        name=(info.get("name") or "").strip(),
        industry=(info.get("industryName") or detail.get("className") or "").strip(),
        founded_year=info.get("foundedYear") or detail.get("foundedYear"),
        location=full_location.strip(),
        employee_count=employee_data.get("total") or detail.get("npsEmployeeCount"),
        avg_salary_manwon=round(salary_raw / 10000) if salary_raw is not None else None,
        hired_1y=employee_data.get("hired") if employee_data.get("hired") is not None else detail.get("hiredCount"),
        left_1y=employee_data.get("left") if employee_data.get("left") is not None else detail.get("leftCount"),
        total_sales_eok=round(sales_raw / 100_000_000, 1) if sales_raw is not None else None,
        sales_year=sales_year,
        tags=tuple(all_tags),
        description=(info.get("description") or "").strip(),
        homepage=(info.get("link") or "").strip(),
    )


def wanted_search_company_id(
    company_name: str,
    *,
    verify_industry: str = "",
    verify_location: str = "",
    http: HttpClient | None = None,
) -> int | None:
    from urllib.parse import quote
    client = http or UrllibHttpClient()
    url = f"{WANTED_BASE_URL}/search?query={quote(company_name)}&tab=company"
    try:
        html = client.request_text(url)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        data = _extract_wanted_next_data(html)
    except ValueError:
        return None

    companies = (
        data.get("props", {})
        .get("pageProps", {})
        .get("dehydrateState", {})
        .get("queries", [])
    )
    if not isinstance(companies, (list, tuple)):
        return None
    for q in companies:
        if not isinstance(q, dict):
            continue
        qk = q.get("queryKey", [])
        if not isinstance(qk, (list, tuple)):
            continue
        if not any("company" in str(k).lower() for k in qk):
            continue
        state = q.get("state")
        if not isinstance(state, dict):
            continue
        state_data = state.get("data")
        if not isinstance(state_data, dict):
            continue
        results = state_data.get("data", [])
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if not name:
                continue
            if _normalize_for_match(name) != _normalize_for_match(company_name):
                continue
            cid = item.get("id")
            if cid is None:
                continue
            if not _verify_corroboration(item, verify_industry, verify_location):
                continue
            try:
                company_id = int(cid)
            except (TypeError, ValueError):
                continue
            if company_id <= 0:
                continue
            return company_id
    return None


def wanted_company_is_valid(info: WantedCompanyInfo) -> bool:
    if int(info.company_id) <= 0:
        return False
    if not _wanted_detail_text_is_safe(info.name):
        return False
    if not _wanted_detail_text_is_safe(info.industry):
        return False
    if not _wanted_detail_text_is_safe(info.location):
        return False
    if not _wanted_metrics_are_valid(info):
        return False
    return True


def wanted_company_matches(
    info: WantedCompanyInfo,
    company_name: str,
    *,
    verify_industry: str = "",
    verify_location: str = "",
) -> bool:
    if not wanted_company_is_valid(info):
        return False
    if _normalize_for_match(info.name) != _normalize_for_match(company_name):
        return False
    if not _verify_corroboration(
        {"industry_name": info.industry, "location": info.location},
        verify_industry,
        verify_location,
    ):
        return False
    return True


def _normalize_for_match(name: str) -> str:
    import unicodedata

    name = unicodedata.normalize("NFC", name)
    name = re.sub(r"[\s\(\)\[\]（）【】·・\-–—]", "", name)
    return name.lower()


def _verify_corroboration(
    item: dict[str, Any], industry: str, location: str
) -> bool:
    if not industry and not location:
        return False
    item_industry = item.get("industry_name") or item.get("industryName") or ""
    item_location = item.get("location") or ""
    if industry and isinstance(item_industry, str) and _industry_matches(industry, item_industry):
        return True
    if location and isinstance(item_location, str) and _location_matches(location, item_location):
        return True
    return False


def _industry_matches(expected: str, actual: str) -> bool:
    expected_tokens = _normalized_label_tokens(expected)
    actual_tokens = _normalized_label_tokens(actual)
    return bool(expected_tokens and actual_tokens and expected_tokens.intersection(actual_tokens))


def _normalized_label_tokens(value: str) -> set[str]:
    exact = _normalize_for_match(value)
    tokens = {
        _normalize_for_match(part)
        for part in re.split(r"[,/&|·・]+", value)
        if _normalize_for_match(part)
    }
    if exact:
        tokens.add(exact)
    return tokens


def _location_matches(expected: str, actual: str) -> bool:
    expected_normalized = _normalize_for_match(expected)
    actual_normalized = _normalize_for_match(actual)
    if not expected_normalized or not actual_normalized:
        return False
    return (
        actual_normalized.startswith(expected_normalized)
        or expected_normalized.startswith(actual_normalized)
    )


def _wanted_detail_text_is_safe(value: str) -> bool:
    if not value:
        return True
    if len(value) > 200:
        return False
    if any(char in value for char in ("\n", "\r", "|")):
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _wanted_metrics_are_valid(info: WantedCompanyInfo) -> bool:
    current_year = datetime.now().year
    if info.founded_year is not None:
        if not isinstance(info.founded_year, int) or isinstance(info.founded_year, bool):
            return False
        if info.founded_year < 1800 or info.founded_year > current_year:
            return False

    for value in (
        info.employee_count,
        info.avg_salary_manwon,
        info.hired_1y,
        info.left_1y,
    ):
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            return False

    count_values = (info.employee_count, info.hired_1y, info.left_1y)
    for value in count_values:
        if value is None:
            continue
        if value < 0 or value > 10_000_000:
            return False

    if info.avg_salary_manwon is not None and (
        info.avg_salary_manwon < 0 or info.avg_salary_manwon > 1_000_000
    ):
        return False

    if info.total_sales_eok is not None:
        if not isinstance(info.total_sales_eok, (int, float)) or isinstance(info.total_sales_eok, bool):
            return False
        if not math.isfinite(info.total_sales_eok):
            return False
        if info.total_sales_eok < 0 or info.total_sales_eok > 1_000_000_000:
            return False

    return True


def format_wanted_company_markdown(info: WantedCompanyInfo) -> str:
    lines = [
        f"# {info.name}",
        "",
        "## 기업 정보",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
    ]
    if info.industry:
        lines.append(f"| 업종 | {info.industry} |")
    if info.founded_year:
        lines.append(f"| 설립 | {info.founded_year}년 |")
    if info.employee_count is not None:
        lines.append(f"| 직원수 | {info.employee_count}명 |")
    if info.location:
        lines.append(f"| 위치 | {info.location} |")
    if info.homepage:
        lines.append(f"| 홈페이지 | {info.homepage} |")

    lines.extend(["", "## 연봉 정보", "", "| 항목 | 금액 |", "|------|------|"])
    if info.avg_salary_manwon is not None:
        lines.append(f"| 평균 연봉 | **{info.avg_salary_manwon:,}만원** |")

    lines.extend(["", "## 인원 통계", "", "| 항목 | 수치 |", "|------|------|"])
    if info.employee_count is not None:
        lines.append(f"| 현재 인원 | {info.employee_count}명 |")
    if info.hired_1y is not None:
        lines.append(f"| 1년간 입사자 | {info.hired_1y}명 |")
    if info.left_1y is not None:
        lines.append(f"| 1년간 퇴사자 | {info.left_1y}명 |")

    if info.total_sales_eok is not None:
        lines.extend(["", "## 매출 추이", "", "| 연도 | 매출 |", "|------|------|"])
        year_label = info.sales_year or "latest"
        lines.append(f"| {year_label} | {info.total_sales_eok}억원 |")

    if info.tags:
        lines.extend(["", "## 태그"])
        for tag in info.tags:
            lines.append(f"- {tag}")

    if info.description:
        lines.extend(["", "## 회사 소개", "", info.description])

    lines.extend([
        "",
        "---",
        "",
        f"*출처: https://www.wanted.co.kr/company/{info.company_id}*",
    ])
    return "\n".join(lines)


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
