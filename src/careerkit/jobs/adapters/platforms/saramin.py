from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, page_fingerprint

SARAMIN_MOBILE_BASE = "https://m.saramin.co.kr"
SARAMIN_HEADERS = {
    "Accept": "application/json, text/html, */*",
    "Referer": f"{SARAMIN_MOBILE_BASE}/",
}
_MAX_PAGES = 5
_EXPERIENCE_RE = re.compile(
    r"경력\s*(\d+)\s*[~-]\s*(\d+)\s*년"
    r"|신입[·\s]*경력"
    r"|경력\s*(\d+)\s*년\s*(?:이상|↑)"
    r"|경력\s*무관"
    r"|신입"
    r"|^경력$"
)


@dataclass(frozen=True)
class SaraminAdapter:
    name: str = "saramin"
    supports_search: bool = True

    def native_role_mapping(self) -> dict[str, object]:
        return {}

    def search(
        self,
        query: str,
        *,
        config: Any,
        state: Any,
        http: HttpClient | None = None,
    ) -> PaginatedItems:
        del state
        http_client = http or config.http_client
        platform = config.platforms[self.name]
        filters = getattr(config, "filters", {})
        min_experience = filters.get("api_min_experience")
        max_experience = filters.get("api_max_experience")
        all_items: list[dict] = []
        seen_pages: set[tuple[str, ...]] = set()
        page = 1
        pages_fetched = 0
        while True:
            if pages_fetched >= _MAX_PAGES:
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    complete=False,
                    pages_fetched=pages_fetched,
                )
            params = {
                "searchword": query,
                "searchType": "search",
                "page": page,
            }
            if min_experience not in (None, "") and max_experience not in (None, ""):
                params["exp_cd"] = 2
                params["exp_min"] = min_experience
                params["exp_max"] = max_experience
                params["exp_none"] = "y"
            query_string = urlencode(params)
            try:
                data = http_client.request_json(
                    f"{SARAMIN_MOBILE_BASE}/search/get-recruit-list?{query_string}",
                    headers=SARAMIN_HEADERS,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                if not all_items:
                    raise
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    complete=False,
                    pages_fetched=pages_fetched,
                )
            inner_html = data.get("innerHTML", "")
            pages_fetched += 1
            if not inner_html or not inner_html.strip():
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    pages_fetched=pages_fetched,
                )
            parsed = parse_search_html(inner_html)
            if not parsed:
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    pages_fetched=pages_fetched,
                )
            fingerprint = page_fingerprint(parsed)
            if fingerprint in seen_pages:
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    complete=False,
                    pages_fetched=pages_fetched,
                )
            seen_pages.add(fingerprint)
            all_items.extend(parsed)
            count_str = data.get("count", "0").replace(",", "")
            try:
                total = int(count_str)
            except ValueError:
                total = 0
            if len(all_items) >= total:
                return PaginatedItems(
                    items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                    pages_fetched=pages_fetched,
                )
            page += 1
            delay = float(config.rate_limits.get(self.name, 0.0))
            if delay > 0:
                time.sleep(delay)

    def _to_candidate(self, item: dict, base_url: str) -> SearchCandidate:
        rec_idx = str(item.get("id", ""))
        return SearchCandidate(
            platform=self.name,
            job_id=rec_idx,
            raw_id=rec_idx,
            title=(item.get("title") or "").strip(),
            company=(item.get("company") or "").strip(),
            experience=(item.get("experience") or "").strip(),
            url=f"{base_url}/zf_user/jobs/relay/view?rec_idx={rec_idx}",
        )


def parse_search_html(inner_html: str) -> list[dict]:
    cards = re.split(r'(?=<div\s+id="list_\d+"\s+class="recruit_container)', inner_html)
    results: list[dict] = []
    for card in cards:
        rec_match = re.search(r'data-rec_idx=(\d+)', card)
        if not rec_match:
            continue
        rec_idx = rec_match.group(1)
        title_match = re.search(r'class="tit">(.*?)</p>', card, re.DOTALL)
        title = ""
        if title_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        company_match = re.search(r'class="corp_name">(.*?)</span>', card, re.DOTALL)
        company = ""
        if company_match:
            company = re.sub(r"<[^>]+>", "", company_match.group(1)).strip()
        meta_match = re.search(r'class="meta">(.*?)</div>', card, re.DOTALL)
        experience = ""
        if meta_match:
            spans = re.findall(r"<span>([^<]+)</span>", meta_match.group(1))
            for span in spans:
                if _EXPERIENCE_RE.search(span):
                    experience = span.strip()
                    break
        results.append({
            "id": rec_idx,
            "title": title,
            "company": company,
            "experience": experience,
        })
    return results


def extract_csn_from_html(html: str) -> str | None:
    match = re.search(r"csn=([A-Za-z0-9+/=]+)", html)
    return match.group(1) if match else None


def _html_fragment_to_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:ul|ol)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_detail_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    pairs = re.findall(
        r'<dt class="tit">([^<]+)</dt>\s*<dd class="desc">\s*(.*?)\s*</dd>',
        html,
        re.DOTALL,
    )
    for label, value in pairs:
        clean = _html_fragment_to_text(value)
        fields[label.strip()] = clean
    if "경력" not in fields:
        meta = re.search(
            r'<meta\s+name="description"\s+content="([^"]*)"',
            html,
        )
        if meta:
            exp = re.search(r"경력:([^,]+)", meta.group(1))
            if exp:
                fields["경력"] = exp.group(1).strip()
    return fields


def extract_jd_body(html: str, job_id: str) -> str:
    pattern = rf"detailContents_{re.escape(job_id)}\s*=\s*\{{\s*contents:\s*'([A-Za-z0-9+/=]+)'"
    match = re.search(pattern, html)
    if not match:
        return ""
    try:
        decoded = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
    except Exception:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", decoded)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<img[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_company_from_detail(html: str) -> str:
    match = re.search(r'class="corp_name[^"]*"[^>]*>([^<]+)', html)
    if match:
        return match.group(1).strip()
    return ""


def extract_position_from_detail(html: str) -> str:
    title_match = re.search(r"<title>([^<]+)</title>", html)
    if not title_match:
        return ""
    raw = title_match.group(1).strip()
    raw = re.sub(r"\s*\(D-\d+\)\s*", " ", raw)
    raw = re.sub(r"\s*-\s*사람인\s*$", "", raw)
    raw = re.sub(r"^\[[^\]]+\]\s*", "", raw)
    return raw.strip()


@dataclass(frozen=True)
class SaraminCompanyInfo:
    name: str = ""
    industry: str = ""
    company_type: str = ""
    founded_date: str = ""
    employee_count: int | None = None
    avg_salary_manwon: int | None = None
    ceo: str = ""
    address: str = ""
    homepage: str = ""


def saramin_company_http(
    csn: str,
    http: HttpClient | None = None,
) -> SaraminCompanyInfo:
    client = http or UrllibHttpClient()
    page_html = client.request_text(
        f"{SARAMIN_MOBILE_BASE}/job-search/company-info-view?csn={csn}",
        headers={"Referer": f"{SARAMIN_MOBILE_BASE}/"},
    )
    info = _parse_company_jsonld(page_html)
    try:
        salary_data = client.request_json(
            f"{SARAMIN_MOBILE_BASE}/job-search/get-average-salary?csn={csn}",
            headers={"Referer": f"{SARAMIN_MOBILE_BASE}/"},
        )
        salary_html = salary_data.get("html", "")
        salary_text = re.sub(r"<[^>]+>", "", salary_html)
        salary_match = re.search(r"(\d[\d,]+)\s*만원", salary_text)
        if salary_match:
            info = SaraminCompanyInfo(
                name=info.name,
                industry=info.industry,
                company_type=info.company_type,
                founded_date=info.founded_date,
                employee_count=info.employee_count,
                avg_salary_manwon=int(salary_match.group(1).replace(",", "")),
                ceo=info.ceo,
                address=info.address,
                homepage=info.homepage,
            )
    except (OSError, RuntimeError, ValueError):
        pass
    return info


def _parse_company_jsonld(html: str) -> SaraminCompanyInfo:
    jsonld_blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    for block in jsonld_blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "Organization":
            continue
        description = data.get("description", "")
        company_type = ""
        type_match = re.search(r"기업형태\s*:\s*([^,|\"]+)", description)
        if type_match:
            company_type = type_match.group(1).strip()
        employees_raw = data.get("numberOfEmployees") or {}
        employee_count = None
        if isinstance(employees_raw, dict):
            try:
                employee_count = int(employees_raw.get("value", 0))
            except (ValueError, TypeError):
                pass
        same_as = data.get("sameAs") or []
        homepage = ""
        if isinstance(same_as, list) and same_as:
            homepage = same_as[0]
        founder = data.get("founder") or {}
        ceo = ""
        if isinstance(founder, dict):
            ceo = (founder.get("name") or "").strip()
        address_raw = data.get("address") or {}
        address = ""
        if isinstance(address_raw, dict):
            address = (address_raw.get("name") or "").strip()
        return SaraminCompanyInfo(
            name=(data.get("legalName") or data.get("name") or "").strip(),
            industry=(data.get("makesOffer", {}).get("name") or "").strip() if isinstance(data.get("makesOffer"), dict) else "",
            company_type=company_type,
            founded_date=(data.get("foundingDate") or "").strip(),
            employee_count=employee_count,
            ceo=ceo,
            address=address,
            homepage=homepage,
        )
    return SaraminCompanyInfo()


def format_company_markdown(info: SaraminCompanyInfo) -> str:
    founded_year = ""
    if info.founded_date and len(info.founded_date) >= 4:
        founded_year = info.founded_date[:4]
    display_type = info.company_type
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
    if display_type:
        lines.append(f"| 기업형태 | {display_type} |")
    lines.append("| 스타트업 여부 | 아니오 |")
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
        lines.extend([
            "",
            "## 연봉 정보",
            "",
            f"평균 연봉 **{info.avg_salary_manwon:,}만원**",
        ])
    lines.extend([
        "",
        "---",
        "",
        f"*추출일: 사람인 모바일 API*",
        f"*출처: {SARAMIN_MOBILE_BASE}*",
        "",
    ])
    return "\n".join(lines)
