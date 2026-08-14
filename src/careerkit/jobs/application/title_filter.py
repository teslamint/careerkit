from __future__ import annotations

import re
from typing import Iterable, Mapping

from careerkit.jobs.application.requirement_manifest import extract_requirement_manifest

_BRACKET_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_BACKEND_KW_RE = re.compile(r"backend|back(?:-|\s|_)?end|server|백엔드|서버", re.IGNORECASE)
_DOMAIN_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "mobile": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\biOS\b", r"\bAndroid\b", r"\bFlutter\b", r"모바일\s*(앱\s*)?(개발|엔지니어)",
    )),
    "ai_ml": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bML\s*Engineer\b", r"\bMLOps\b", r"\bAI\s*Engineer\b", r"\bLLM\b", r"\bMachine\s*Learning\b",
    )),
    "hardware_embedded": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bSoC\b", r"\bFPGA\b", r"\bEmbedded\b", r"\bHardware\b", r"반도체", r"\bFirmware\b",
    )),
    "devops_sre": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bDevOps\b", r"\bSRE\b", r"Cloud\s*Engineer", r"Platform\s*Engineer", r"인프라\s*엔지니어",
    )),
    "frontend": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bFrontend\b", r"\bFront[\s-]?end\b", r"프론트엔드", r"프론트\s*개발",
    )),
    "non_sw": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"기구설계", r"기계\s*엔지니어", r"전기\s*엔지니어", r"토목", r"\bRF\s*Engineer\b",
    )),
    "data_engineering": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bData\s*Engineer\b", r"데이터\s*엔지니어", r"\bDataOps\b",
    )),
    "qa_pm": tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
        r"\bQA\s*Engineer\b", r"\bProduct\s*Manager\b", r"기획자", r"프로덕트\s*매니저",
    )),
}
_DOMAIN_COUNTER_PATTERNS = {
    "frontend": re.compile(r"full[\s-]?stack|풀스택", re.IGNORECASE),
}
# Exclusion keywords that state a seniority level, not a role. The requirement
# confirmation must not cancel these: backend work in the body does not make a
# 신입 posting a senior one.
_LEVEL_EXCLUSION_RE = re.compile(r"인턴|신입|주니어|junior|entry[\s-]?level|체험형", re.IGNORECASE)


def normalize_job_query(query: str) -> str:
    normalized = query.strip()
    query_lower = normalized.lower()
    if "백엔드" in normalized:
        return "백엔드"
    if re.search(r"\bback(?:-|\s)?end\b", query_lower):
        return "Backend"
    if "서버" in normalized:
        return "서버"
    if re.search(r"\bserver\b", query_lower):
        return "Server"
    return normalized


def normalize_job_queries(queries: list[str]) -> list[str]:
    return list(dict.fromkeys(normalize_job_query(query) for query in queries))


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def _quick_filters(config: Mapping[str, object]) -> Mapping[str, object]:
    quick_filters = config.get("quick_filters", {})
    if isinstance(quick_filters, Mapping):
        return quick_filters
    return {}


def matched_title_exclusion(title: str, config: Mapping[str, object]) -> str | None:
    """The strongest `title_exclude` keyword that cut this title, if one did.

    When a title matches both a seniority keyword and a role keyword (e.g.
    "신입 Frontend Backend Engineer"), the seniority keyword wins: it is evidence
    the body cannot contradict, and a list ordering that puts the role keyword
    first must not let the body cancel the cut. A role-only match returns the
    first role keyword — ordering is immaterial there, since any role keyword
    is equally cancellable.
    """
    title_lower = title.lower()
    first_match: str | None = None
    for keyword in _string_list(_quick_filters(config).get("title_exclude", [])):
        if keyword.lower() in title_lower:
            if is_level_exclusion(keyword):
                return keyword
            if first_match is None:
                first_match = keyword
    return first_match


def is_level_exclusion(keyword: str) -> bool:
    """Does this exclusion keyword name a seniority level rather than a role?

    Backend requirements cannot contradict it. A 신입 posting whose body describes
    backend work is still a 신입 posting, so the body must not cancel the cut — the
    same reason `closed` and `prior_application` are never cancelled. Role and
    domain keywords are different: they are a guess about what the job *is*, and
    the requirements are better evidence than the title.
    """
    return bool(_LEVEL_EXCLUSION_RE.search(keyword))


def quick_filter_title(title: str, config: Mapping[str, object]) -> str | None:
    filters = _quick_filters(config)
    title_lower = title.lower()

    for keyword in _string_list(filters.get("title_exclude", [])):
        if keyword.lower() in title_lower:
            return "pass"

    include_keywords = _string_list(filters.get("title_include", []))
    if include_keywords and not any(keyword.lower() in title_lower for keyword in include_keywords):
        return "pass"

    for keyword in _string_list(filters.get("title_prefer", [])):
        if keyword.lower() in title_lower:
            return "prefer"

    return None


def strip_bracket_prefix(title: str) -> str:
    return _BRACKET_PREFIX_RE.sub("", title).strip()


def has_backend_keyword(title: str) -> bool:
    return bool(_BACKEND_KW_RE.search(strip_bracket_prefix(title)))


def requirements_show_backend(jd_markdown: str) -> bool:
    # Every item, not just the parents: a requirement whose outer bullet is generic
    # ("아래 항목 중 하나 이상에 해당하는 분") keeps its backend evidence in an indented child,
    # and scanning parents alone sets that posting aside. Re-measured over the corpus
    # when this widened — the disputed set's outcomes did not move.
    manifest = extract_requirement_manifest(jd_markdown)
    return any(has_backend_keyword(item.text) for item in manifest.items)


def classify_non_backend_domain(title: str) -> str | None:
    for category, patterns in _DOMAIN_PATTERNS.items():
        if any(pattern.search(title) for pattern in patterns):
            return category
    return None


def has_domain_counter_indicator(title: str, category: str) -> bool:
    if has_backend_keyword(title):
        return True
    pattern = _DOMAIN_COUNTER_PATTERNS.get(category)
    return bool(pattern and pattern.search(title))
