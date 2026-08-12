from __future__ import annotations

import json
import time
from dataclasses import dataclass

from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
from careerkit.jobs.adapters.platforms._next_data import extract_next_data, find_query_by_key
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, page_fingerprint

REMEMBER_API_BASE = "https://career-api.rememberapp.co.kr"
REMEMBER_BASE_URL = "https://career.rememberapp.co.kr"
REMEMBER_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": REMEMBER_BASE_URL,
    "Referer": f"{REMEMBER_BASE_URL}/",
}
REMEMBER_BACKEND_MAPPING = {"job_category_names": [{"level1": "SW개발", "level2": "백엔드"}]}
_DEFAULT_PER = 30
_MAX_PAGES = 1000


@dataclass(frozen=True)
class RememberAdapter:
    name: str = "remember"
    supports_search: bool = True

    def native_role_mapping(self) -> dict[str, object]:
        return {"job_category_names": [dict(item) for item in REMEMBER_BACKEND_MAPPING["job_category_names"]]}

    def search(self, query: str, *, config, state, http: HttpClient | None = None) -> PaginatedItems:
        del state
        http_client = http or config.http_client
        platform = config.platforms[self.name]
        page = 1
        pages_fetched = 0
        seen_pages: set[tuple[str, ...]] = set()
        all_items: list[dict] = []
        total_count = 0
        while True:
            if pages_fetched >= _MAX_PAGES:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total_count, complete=False, pages_fetched=pages_fetched)
            search = {
                "keywords": [query],
                "min_experience": int(config.filters.get("api_min_experience", 10)),
                "max_experience": int(config.filters.get("api_max_experience", 15)),
                "job_category_names": self.native_role_mapping()["job_category_names"],
            }
            body = json.dumps({"page": page, "per": _DEFAULT_PER, "search": search}).encode("utf-8")
            try:
                data = http_client.request_json(f"{REMEMBER_API_BASE}/job_postings/search", headers=REMEMBER_HEADERS, method="POST", body=body)
            except (OSError, RuntimeError, ValueError):
                if not all_items:
                    raise
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total_count, complete=False, pages_fetched=pages_fetched)
            items = data.get("data", [])
            meta = data.get("meta", {})
            total_count = int(meta.get("total_count", 0))
            total_pages = int(meta.get("total_pages", 1))
            pages_fetched += 1
            if not items:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total_count, pages_fetched=pages_fetched)
            fingerprint = page_fingerprint(items)
            if fingerprint in seen_pages:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total_count, complete=False, pages_fetched=pages_fetched)
            seen_pages.add(fingerprint)
            all_items.extend(items)
            if page >= total_pages:
                return PaginatedItems(items=tuple(self._to_candidate(item, platform.base_url) for item in all_items), total_count=total_count, pages_fetched=pages_fetched)
            page += 1
            delay = float(config.rate_limits.get(self.name, 0.0))
            if delay > 0:
                time.sleep(delay)

    def _to_candidate(self, item: dict, base_url: str) -> SearchCandidate:
        raw_id = str(item.get("id"))
        organization = item.get("organization") or {}
        experience = _format_experience(item)
        return SearchCandidate(platform=self.name, job_id=raw_id, raw_id=raw_id, title=(item.get("title") or "").strip(), company=(organization.get("name") or "").strip(), experience=str(experience).strip(), url=f"{base_url}/job/posting/{raw_id}")


@dataclass(frozen=True)
class RememberCompanyInfo:
    company_id: int
    name: str
    address: str
    industry: str
    established: str
    employee_count: int | None
    avg_salary_manwon: int | None
    salary_yoy_change: int | None
    employee_stats: tuple[dict, ...]
    company_type: str
    homepage: str
    ceo: str
    tags: tuple[str, ...]


def remember_company_http(
    company_id: int | str,
    http: HttpClient | None = None,
) -> RememberCompanyInfo:
    client = http or UrllibHttpClient()
    url = f"{REMEMBER_BASE_URL}/job/company/{company_id}"
    html = client.request_text(url)
    data = extract_next_data(html)
    company = find_query_by_key(data, "/companies/")
    if not isinstance(company, dict) or "name" not in company:
        raise ValueError(f"invalid company data for {company_id}")

    salary = company.get("salaryStatistics") or {}
    avg_raw = salary.get("average")
    yoy_raw = salary.get("changesFromLastYear")

    stats_raw = company.get("employeeStatistics") or []
    _stat_keys = ("month", "total", "join", "leave")
    stats = tuple(
        {"month": s["month"], "total": s["total"], "join": s["join"], "leave": s["leave"]}
        for s in stats_raw
        if isinstance(s, dict) and all(k in s for k in _stat_keys)
    )

    ind = company.get("industry") or {}
    industry_parts = [ind.get(k, "") for k in ("level1", "level2", "level3") if ind.get(k)]

    return RememberCompanyInfo(
        company_id=int(company.get("id", company_id)),
        name=(company.get("name") or "").strip(),
        address=(company.get("address") or "").strip(),
        industry=" > ".join(industry_parts),
        established=(company.get("establishmentDate") or "").strip(),
        employee_count=stats[0]["total"] if stats else None,
        avg_salary_manwon=round(avg_raw / 10000) if avg_raw is not None else None,
        salary_yoy_change=round(yoy_raw / 10000) if yoy_raw is not None else None,
        employee_stats=stats,
        company_type=(company.get("type") or "").strip(),
        homepage=(company.get("homepageUrl") or "").strip(),
        ceo=(company.get("representativeName") or "").strip(),
        tags=tuple(company.get("tags") or []),
    )


def format_company_markdown(info: RememberCompanyInfo) -> str:
    founded_year = ""
    if info.established and len(info.established) >= 4:
        founded_year = info.established[:4]
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
    if info.company_type:
        lines.append(f"| 기업형태 | {info.company_type} |")
    if founded_year:
        lines.append(f"| 설립 | {founded_year}년 |")
    if info.employee_count is not None:
        lines.append(f"| 직원수 | {info.employee_count:,}명 |")
    if info.ceo:
        lines.append(f"| 대표자 | {info.ceo} |")
    if info.address:
        lines.append(f"| 주소 | {info.address} |")
    if info.homepage:
        lines.append(f"| 홈페이지 | {info.homepage} |")
    if info.avg_salary_manwon is not None:
        lines.extend(["", "## 연봉 정보", ""])
        lines.append(f"평균 연봉 **{info.avg_salary_manwon:,}만원**")
        if info.salary_yoy_change is not None:
            sign = "+" if info.salary_yoy_change > 0 else ""
            lines.append(f"작년 대비 {sign}{info.salary_yoy_change:,}만원")
    if info.employee_stats:
        latest = info.employee_stats[0]
        join_total = sum(s.get("join", 0) for s in info.employee_stats)
        leave_total = sum(s.get("leave", 0) for s in info.employee_stats)
        lines.extend([
            "",
            "## 인원 통계",
            "",
            "| 항목 | 수치 |",
            "|------|------|",
            f"| 현재 인원 | {latest['total']:,}명 |",
            f"| 1년간 입사자 | {join_total}명 |",
            f"| 1년간 퇴사자 | {leave_total}명 |",
        ])
    if info.tags:
        lines.extend(["", "## 태그", ""])
        for tag in info.tags:
            lines.append(f"- {tag}")
    lines.extend([
        "",
        "---",
        "",
        f"*추출일: 리멤버 HTTP API*",
        f"*출처: {REMEMBER_BASE_URL}/job/company/{info.company_id}*",
        "",
    ])
    return "\n".join(lines)


def _format_experience(item: dict) -> str:
    minimum = item.get("min_experience")
    maximum = item.get("max_experience")
    if minimum == 0 and maximum == 0:
        return "경력 무관"
    if minimum not in (None, "") and maximum not in (None, ""):
        return f"{minimum}~{maximum}년"
    if minimum not in (None, ""):
        return f"{minimum}년 이상"
    if maximum not in (None, ""):
        return f"{maximum}년 이하"
    return str(item.get("career") or item.get("experience") or "")
