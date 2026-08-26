from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from careerkit.jobs.adapters.http import HttpClient, HttpStatusError, UrllibHttpClient

THEVC_API_BASE = "https://thevc.kr/api/information/organizations/profiles"
THEVC_BASE_URL = "https://thevc.kr"


def _is_paywall(value: object) -> bool:
    return isinstance(value, dict) and "requirements" in value


@dataclass(frozen=True)
class TheVCFundingRound:
    round_name: str = ""
    funded_on: str = ""
    funding_type: str = ""


@dataclass(frozen=True)
class TheVCCompanyInfo:
    name: str = ""
    name_en: str = ""
    founded_on: str = ""
    corp_type: str = ""
    status: str = ""
    address: str = ""
    website: str = ""
    ceo_name: str = ""
    ceo_is_founder: bool = False
    keywords: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    last_round: str = ""
    last_funded_on: str = ""
    total_funding_count: int = 0
    investor_count_total: int = 0
    funding_rounds: tuple[TheVCFundingRound, ...] = ()
    slug: str = ""


@dataclass(frozen=True)
class TheVCAdapter:
    name: str = "thevc"
    supports_search: bool = False

    def native_role_mapping(self) -> dict[str, object]:
        return {}


def _safe_str(value: object) -> str:
    if _is_paywall(value) or value is None:
        return ""
    return str(value).strip()


def _safe_int(value: object) -> int:
    if _is_paywall(value) or value is None:
        return 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _parse_date(value: object) -> str:
    """Return the calendar date in KST (UTC+9) for an ISO UTC timestamp.

    KST has no DST, so a UTC time >= 15:00:00 (09 h before midnight) maps to
    the next KST calendar day; earlier UTC times keep the same day.
    Unparseable values fall back to the date component.
    """
    s = _safe_str(value)
    if not s or "T" not in s:
        return s
    date_part, _ = s.split("T", 1)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return date_part
    kst = dt.astimezone(timezone(timedelta(hours=9), name="Asia/Seoul"))
    return kst.date().isoformat()


def _parse_funding_rounds(fundings: list) -> tuple[TheVCFundingRound, ...]:
    rounds: list[TheVCFundingRound] = []
    for f in fundings:
        if not isinstance(f, dict):
            continue
        rounds.append(
            TheVCFundingRound(
                round_name=_safe_str(f.get("round")),
                funded_on=_parse_date(f.get("fundedOn")),
                funding_type=_safe_str(f.get("type")),
            )
        )
    return tuple(rounds)


def thevc_company_http(
    slug: str,
    http: HttpClient | None = None,
) -> TheVCCompanyInfo:
    client = http or UrllibHttpClient()
    encoded_slug = urllib.parse.quote(slug, safe="")
    url = f"{THEVC_API_BASE}/{encoded_slug}"
    try:
        data = client.request_json(url)
    except HttpStatusError as exc:
        if exc.status == 404:
            raise ValueError(f"TheVC company not found: {slug}") from exc
        raise
    if not isinstance(data, dict):
        raise ValueError(f"TheVC returned unexpected response for: {slug}")

    members = data.get("members")
    if not isinstance(members, list):
        members = []
    ceo_name = ""
    ceo_is_founder = False
    for member in members:
        if isinstance(member, dict) and member.get("isCEO"):
            ceo_name = _safe_str(member.get("name"))
            ceo_is_founder = bool(member.get("isFounder"))
            break

    raw_keywords = data.get("relatedKeywords")
    if not isinstance(raw_keywords, list):
        raw_keywords = []
    keywords = tuple(k for k in raw_keywords if isinstance(k, str))

    raw_products = data.get("products")
    if not isinstance(raw_products, list):
        raw_products = []
    products: list[str] = []
    for p in raw_products:
        if isinstance(p, dict):
            pname = _safe_str(p.get("name"))
            if pname:
                desc = _safe_str(p.get("desc"))
                products.append(f"{pname} — {desc}" if desc else pname)

    fundings = data.get("fundings")
    if not isinstance(fundings, list):
        fundings = []
    funding_rounds = _parse_funding_rounds(fundings)

    investor_count_raw = data.get("investorCount")
    investor_count_total = 0
    if isinstance(investor_count_raw, dict) and not _is_paywall(investor_count_raw):
        try:
            investor_count_total = int(investor_count_raw.get("total", 0))
        except (TypeError, ValueError, OverflowError):
            pass

    return TheVCCompanyInfo(
        name=_safe_str(data.get("name")),
        name_en=_safe_str(data.get("nameEn")),
        founded_on=_parse_date(data.get("foundedOn")),
        corp_type=_safe_str(data.get("corpType")),
        status=_safe_str(data.get("status")),
        address=_safe_str(data.get("address")),
        website=_safe_str(data.get("website")),
        ceo_name=ceo_name,
        ceo_is_founder=ceo_is_founder,
        keywords=keywords,
        products=tuple(products),
        last_round=_safe_str(data.get("lastRound")),
        last_funded_on=_parse_date(data.get("lastFundedOn")),
        total_funding_count=_safe_int(data.get("totalFundingCount")),
        investor_count_total=investor_count_total,
        funding_rounds=funding_rounds,
        slug=slug,
    )


def format_thevc_company_markdown(info: TheVCCompanyInfo) -> str:
    lines = [
        f"# {info.name}",
        "",
        "## 기업 정보",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
    ]
    if info.ceo_name:
        founder_mark = " (창업자)" if info.ceo_is_founder else ""
        lines.append(f"| 대표자 | {info.ceo_name}{founder_mark} |")
    if info.founded_on:
        lines.append(f"| 설립일 | {info.founded_on} |")
    if info.corp_type:
        lines.append(f"| 기업형태 | {info.corp_type} |")
    if info.status:
        lines.append(f"| 상장여부 | {info.status} |")
    lines.append("| 스타트업 여부 | yes |")
    if info.address:
        lines.append(f"| 주소 | {info.address} |")
    if info.website:
        lines.append(f"| 홈페이지 | {info.website} |")
    if info.keywords:
        lines.append(f"| 키워드 | {', '.join(info.keywords)} |")
    if info.products:
        lines.append(f"| 주요 서비스 | {'; '.join(info.products)} |")

    has_funding_info = any((
        info.funding_rounds,
        info.last_round,
        info.last_funded_on,
        info.total_funding_count,
        info.investor_count_total,
    ))
    if has_funding_info:
        lines.extend(["", "## 투자 정보", "", "| 항목 | 내용 |", "|------|------|"])
        if info.last_round:
            lines.append(f"| 현재 라운드 | {info.last_round} |")
        if info.last_funded_on:
            lines.append(f"| 최근 투자일 | {info.last_funded_on} |")
        if info.total_funding_count:
            lines.append(f"| 총 라운드 수 | {info.total_funding_count}회 |")
        if info.investor_count_total:
            lines.append(f"| 누적 투자자 수 | {info.investor_count_total}곳 |")

        if info.funding_rounds:
            lines.extend(["", "### 투자 이력", ""])
            lines.append("| 라운드 | 날짜 | 유형 |")
            lines.append("|--------|------|------|")
            for r in info.funding_rounds:
                lines.append(f"| {r.round_name} | {r.funded_on} | {r.funding_type} |")

    lines.extend([
        "",
        "---",
        "",
        "*출처:*",
        f"- {THEVC_BASE_URL}/{info.slug}",
    ])
    return "\n".join(lines)
