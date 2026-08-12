from __future__ import annotations

import base64
from collections.abc import Mapping
import html as html_lib
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from careerkit.jobs.adapters.http import HttpClient, UrllibHttpClient
from careerkit.jobs.application.search import PaginatedItems, SearchCandidate, StopReason, page_fingerprint

SARAMIN_MOBILE_BASE = "https://m.saramin.co.kr"
SARAMIN_HEADERS = {
    "Accept": "application/json, text/html, */*",
    "Referer": f"{SARAMIN_MOBILE_BASE}/",
}
_MAX_PAGES = 1000
_MAX_SECONDS = 600
_EXPERIENCE_RE = re.compile(
    r"경력\s*(\d+)\s*[~-]\s*(\d+)\s*년"
    r"|신입[·\s]*경력"
    r"|경력\s*(\d+)\s*년\s*(?:이상|↑)"
    r"|경력\s*무관"
    r"|신입"
    r"|^경력$"
)


def _normalize_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", "", value).replace(",", "")
    if not re.fullmatch(r"[0-9]+", normalized):
        return None
    try:
        return int(normalized)
    except ValueError:
        return None




@dataclass(frozen=True)
class SaraminJDSections:
    introduction: tuple[str, ...] = ()
    main_duties: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    preferred: tuple[str, ...] = ()


_SECTION_ALIASES = {
    "주요업무": "main_duties",
    "담당업무": "main_duties",
    "업무내용": "main_duties",
    "자격요건": "requirements",
    "지원자격": "requirements",
    "필수요건": "requirements",
    "필수사항": "requirements",
    "필요역량/경험": "requirements",
    "우대사항": "preferred",
    "우대조건": "preferred",
    "우대요건": "preferred",
    "선호역량/경험": "preferred",
}
_SECTION_BOUNDARIES = {
    "근무조건",
    "근무환경",
    "복리후생",
    "혜택",
    "혜택및복지",
    "복지및혜택",
    "전형절차",
    "채용절차",
    "마감일및근무지",
    "회사소개",
    "기업소개",
}
_SOURCE_BULLET_RE = re.compile(r"^(?:[-*+•◦]\s*)+")


def _empty_sections() -> SaraminJDSections:
    return SaraminJDSections()


def _append_section(sections: dict[str, list[str]], section_name: str, values: list[str]) -> None:
    if not values:
        return
    sections[section_name].extend(values)


def _normalize_saramin_heading(text: str) -> str:
    clean = html_lib.unescape(re.sub(r"<[^>]+>", " ", text)).strip()
    clean = re.sub(r"^[^\w]+", "", clean)
    previous = None
    while clean != previous:
        previous = clean
        clean = re.sub(r"^\d+\s*[.)]\s*", "", clean)
        clean = re.sub(r"^[\[(（]+\s*", "", clean)
        clean = re.sub(r"\s*[\])）]+$", "", clean)
    return re.sub(r"\s+", "", clean).strip().rstrip(":：")


def _fragment_lines(fragment: str) -> list[str]:
    normalized = fragment
    normalized = re.sub(r"<br\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<(?:li)[^>]*>", "\n- ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"</(?:li|p|div|ul|ol|table|tr|section)>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<(?:p|div|ul|ol|table|tr|section)[^>]*>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = html_lib.unescape(normalized)
    lines: list[str] = []
    for raw_line in normalized.splitlines():
        clean = re.sub(r"\s+", " ", raw_line).strip()
        if clean:
            lines.append(clean)
    return lines




def _strip_source_bullet(line: str) -> str:
    return _SOURCE_BULLET_RE.sub("", line).strip()


def _section_lines(fragment: str, target: str, *, from_detail: bool) -> list[str]:
    lines = _fragment_lines(fragment)
    if target == "introduction":
        return [clean for clean in (_strip_source_bullet(line) for line in lines) if clean]
    if from_detail or target in {"main_duties", "preferred"}:
        formatted: list[str] = []
        for line in lines:
            clean = _strip_source_bullet(line)
            if clean:
                formatted.append(f"- {clean}")
        return formatted
    return [line for line in lines if _strip_source_bullet(line)]

def _body_html(html: str, job_id: str) -> str:
    pattern = rf"detailContents_{re.escape(job_id)}\s*=\s*\{{\s*contents:\s*'([A-Za-z0-9+/=]+)'"
    match = re.search(pattern, html)
    if not match:
        return ""
    try:
        decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8")
    except Exception:
        return ""
    if "<" not in decoded:
        return ""
    return decoded


def _sections_from_body_lines(body_html: str) -> SaraminJDSections:
    sections: dict[str, list[str]] = {
        "introduction": [],
        "main_duties": [],
        "requirements": [],
        "preferred": [],
    }
    active_section: str | None = None
    saw_section = False
    for line in _fragment_lines(body_html):
        heading = _normalize_saramin_heading(line)
        if heading in _SECTION_ALIASES:
            active_section = _SECTION_ALIASES[heading]
            saw_section = True
            continue
        if heading in _SECTION_BOUNDARIES:
            active_section = None
            continue
        item = _strip_source_bullet(line)
        if active_section is None:
            if not saw_section and item:
                sections["introduction"].append(item)
            continue
        if item:
            sections[active_section].append(f"- {item}")
    return SaraminJDSections(
        introduction=tuple(sections["introduction"]) if saw_section else (),
        main_duties=tuple(sections["main_duties"]),
        requirements=tuple(sections["requirements"]),
        preferred=tuple(sections["preferred"]),
    )


def _sections_from_body_html(body_html: str) -> SaraminJDSections:
    if not body_html:
        return _empty_sections()
    matches = []
    for match in re.finditer(r"<(h[1-6]|strong|b)[^>]*>(.*?)</\1>", body_html, re.IGNORECASE | re.DOTALL):
        tag_name = match.group(1).lower()
        if tag_name.startswith("h"):
            matches.append(match)
            continue
        if _normalize_saramin_heading(match.group(2)) in _SECTION_ALIASES:
            matches.append(match)
    if not matches:
        return _sections_from_body_lines(body_html)
    sections: dict[str, list[str]] = {
        "introduction": [],
        "main_duties": [],
        "requirements": [],
        "preferred": [],
    }
    _append_section(sections, "introduction", _section_lines(body_html[: matches[0].start()], "introduction", from_detail=False))
    for index, match in enumerate(matches):
        heading = _normalize_saramin_heading(match.group(2))
        target = _SECTION_ALIASES.get(heading, "introduction")
        content_start = match.end()
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(body_html)
        _append_section(sections, target, _section_lines(body_html[content_start:content_end], target, from_detail=False))
    structured_sections = SaraminJDSections(
        introduction=tuple(sections["introduction"]),
        main_duties=tuple(sections["main_duties"]),
        requirements=tuple(sections["requirements"]),
        preferred=tuple(sections["preferred"]),
    )
    line_sections = _sections_from_body_lines(body_html)
    line_section_count = sum(
        bool(section)
        for section in (line_sections.main_duties, line_sections.requirements, line_sections.preferred)
    )
    structured_section_count = sum(
        bool(section)
        for section in (structured_sections.main_duties, structured_sections.requirements, structured_sections.preferred)
    )
    return line_sections if line_section_count > structured_section_count else structured_sections


def extract_jd_sections(html: str, job_id: str) -> SaraminJDSections:
    return _sections_from_body_html(_body_html(html, job_id))


def extract_detail_sections(html: str) -> SaraminJDSections:
    pairs = re.findall(
        r'<dt class="tit">([^<]+)</dt>\s*<dd class="desc">\s*(.*?)\s*</dd>',
        html,
        re.DOTALL,
    )
    sections: dict[str, list[str]] = {
        "introduction": [],
        "main_duties": [],
        "requirements": [],
        "preferred": [],
    }
    for label, value in pairs:
        target = _SECTION_ALIASES.get(_normalize_saramin_heading(label))
        if target is None:
            continue
        _append_section(sections, target, _section_lines(value, target, from_detail=True))
    return SaraminJDSections(
        introduction=tuple(sections["introduction"]),
        main_duties=tuple(sections["main_duties"]),
        requirements=tuple(sections["requirements"]),
        preferred=tuple(sections["preferred"]),
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
        seen_ids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        page = 1
        pages_fetched = 0
        deadline = time.monotonic() + _MAX_SECONDS
        observed_count: int | None = None
        saw_count = False
        counts_valid = True

        def result(*, complete: bool, stop_reason: StopReason) -> PaginatedItems:
            total_count = observed_count if saw_count and counts_valid else None
            return PaginatedItems(
                items=tuple(self._to_candidate(item, platform.base_url) for item in all_items),
                total_count=total_count,
                complete=complete,
                pages_fetched=pages_fetched,
                stop_reason=stop_reason,
            )

        while True:
            if pages_fetched >= _MAX_PAGES:
                return result(complete=False, stop_reason="safety_page_limit")
            now = time.monotonic()
            if now >= deadline:
                return result(complete=False, stop_reason="safety_time_limit")
            request_timeout = max(1, min(15, int(deadline - now)))
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
                    timeout=request_timeout,
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError):
                if not all_items:
                    raise
                return result(complete=False, stop_reason="request_error")
            pages_fetched += 1
            if time.monotonic() >= deadline:
                return result(complete=False, stop_reason="safety_time_limit")
            if not isinstance(data, Mapping):
                counts_valid = False
                return result(complete=False, stop_reason="malformed_response")
            count = _normalize_count(data.get("count"))
            if count is None:
                counts_valid = False
            elif not saw_count:
                observed_count = count
                saw_count = True
            elif observed_count != count:
                counts_valid = False
            inner_html = data.get("innerHTML")
            if not isinstance(inner_html, str):
                counts_valid = False
                return result(complete=False, stop_reason="malformed_response")
            if not inner_html.strip():
                return result(complete=True, stop_reason="api_end")
            parsed = parse_search_html(inner_html)
            if not parsed:
                return result(complete=False, stop_reason="malformed_page")
            fingerprint = page_fingerprint(parsed)
            if fingerprint in seen_pages:
                return result(complete=False, stop_reason="repeated_page")
            seen_pages.add(fingerprint)
            new_items: list[dict] = []
            for item in parsed:
                item_id = str(item.get("id", ""))
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                new_items.append(item)
            if not new_items:
                return result(complete=False, stop_reason="no_new_items")
            all_items.extend(new_items)
            page += 1
            if pages_fetched >= _MAX_PAGES:
                return result(complete=False, stop_reason="safety_page_limit")
            delay = float(config.rate_limits.get(self.name, 0.0))
            if delay > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return result(complete=False, stop_reason="safety_time_limit")
                time.sleep(min(delay, remaining))

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


def extract_detail_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    pairs = re.findall(
        r'<dt class="tit">([^<]+)</dt>\s*<dd class="desc">\s*(.*?)\s*</dd>',
        html,
        re.DOTALL,
    )
    for label, value in pairs:
        clean = re.sub(r"<[^>]+>", " ", value)
        clean = re.sub(r"\s+", " ", clean).strip()
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
    decoded = _body_html(html, job_id)
    if not decoded:
        return ""
    return "\n".join(_fragment_lines(decoded))


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
