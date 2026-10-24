"""Parse a 직행 (zighang.com) detail page that the browser extension captured.

zighang.com and api.zighang.com disallow automated access in robots.txt, so this
module never fetches anything: it reads only the HTML the user's own browser loaded
(see docs/adr/0001-zighang-extension-only-collection.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from careerkit.jobs.application.storage_migration import posting_key_from_url

ZIGHANG_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_RECRUITMENT_RE = re.compile(r'"recruitment"\s*:\s*\{')
_UNBOUNDED_CAREER_MAX = 100
_SECTION_HEADINGS = {
    "담당업무": "main_duties",
    "주요업무": "main_duties",
    "필요역량": "requirements",
    "자격요건": "requirements",
    "지원자격": "requirements",
    "우대사항": "preferred",
    "보상및복리후생": "benefits",
    "급여및복리후생": "benefits",
    "복리후생": "benefits",
    "혜택및복지": "benefits",
}
_LIST_TYPES = {"bulletList", "orderedList"}


@dataclass(frozen=True)
class ZighangPosting:
    job_id: str
    title: str
    company_name: str
    experience: str
    location: str
    introduction: str
    main_duties: str
    requirements: str
    preferred: str
    benefits: str
    used_summary: bool
    origin_candidates: tuple[str, ...]


def _inline_text(node: object) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if node.get("type") == "hardBreak":
        return "\n"
    children = node.get("content")
    return "".join(_inline_text(child) for child in children) if isinstance(children, list) else ""


def _block_lines(node: object, depth: int = 0) -> list[str]:
    if not isinstance(node, dict) or node.get("type") == "image":
        return []
    content = node.get("content")
    children = content if isinstance(content, list) else []
    if node.get("type") in {"paragraph", "heading"}:
        return [line.strip() for line in _inline_text(node).splitlines() if line.strip()]
    if node.get("type") in _LIST_TYPES:
        lines: list[str] = []
        for item in children:
            item_content = item.get("content") if isinstance(item, dict) else None
            item_children = item_content if isinstance(item_content, list) else []
            bulleted = False
            for child in item_children:
                if isinstance(child, dict) and child.get("type") in _LIST_TYPES:
                    lines.extend(_block_lines(child, depth + 1))
                    continue
                for line in _block_lines(child):
                    lines.append("  " * depth + ("  " if bulleted else "- ") + line)
                    bulleted = True
        return lines
    return [line for child in children for line in _block_lines(child, depth)]


def tiptap_to_text(doc: object) -> str:
    return "\n".join(_block_lines(doc)).strip()


def _sections(doc: dict) -> dict[str, str]:
    buckets: dict[str, list[str]] = {"introduction": [], "main_duties": [], "requirements": [], "preferred": [], "benefits": []}
    current = "introduction"
    content = doc.get("content")
    children = content if isinstance(content, list) else []
    for child in children:
        if isinstance(child, dict) and child.get("type") == "heading":
            heading = _inline_text(child).strip()
            current = _SECTION_HEADINGS.get(re.sub(r"\s+", "", heading), "introduction")
            if current == "introduction" and heading:
                buckets[current].append(heading)
            continue
        buckets[current].extend(_block_lines(child))
    return {key: "\n".join(lines).strip() for key, lines in buckets.items()}


# RSC encodes a literal leading "$" as "$$", so a raw "$" string is an unresolved
# reference ("$undefined", "$5") rather than data; every field read goes through here.
def _field(mapping: dict, key: str) -> object:
    value = mapping.get(key)
    if isinstance(value, str) and value.startswith("$"):
        raise ValueError(f"zighang recruitment {key} is an unresolved reference")
    return value


def _doc(recruitment: dict, key: str) -> dict | None:
    value = _field(recruitment, key)
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"zighang recruitment {key} is not a document") from exc
    if not isinstance(value, dict):
        raise ValueError(f"zighang recruitment {key} is not a document")
    return value


def format_zighang_experience(career_min: int | None, career_max: int | None) -> str:
    if not isinstance(career_min, int) or not isinstance(career_max, int) or isinstance(career_min, bool) or isinstance(career_max, bool):
        return ""
    if career_min < 0 or career_max < career_min:
        return ""
    if career_max == 0:
        return "신입"
    if career_max >= _UNBOUNDED_CAREER_MAX:
        return "경력 무관" if career_min == 0 else f"{career_min}년 이상"
    return f"{career_min}~{career_max}년"


def _payload(page: str) -> str:
    chunks = []
    for match in _PUSH_RE.finditer(page):
        try:
            chunks.append(json.loads(f'"{match.group(1)}"'))
        except json.JSONDecodeError as exc:
            raise ValueError("zighang page has an undecodable payload chunk") from exc
    return "".join(chunks)


def _complete(value: dict) -> bool:
    company = value.get("company")
    return (
        isinstance(value.get("title"), str)
        and isinstance(company, dict)
        and isinstance(company.get("name"), str)
        and (value.get("content") is not None or value.get("summary") is not None)
    )


# The detail page also carries related-posting cards and objects nested inside the main
# one, so only complete objects for the requested id count, and a match inside a span
# already decoded is part of that object rather than a second candidate.
def _recruitment(payload: str, job_id: str) -> dict:
    decoder = json.JSONDecoder()
    found: list[dict] = []
    decoded_end = 0
    for match in _RECRUITMENT_RE.finditer(payload):
        if match.start() < decoded_end:
            continue
        try:
            value, decoded_end = decoder.raw_decode(payload, match.end() - 1)
        except json.JSONDecodeError as exc:
            raise ValueError("zighang recruitment payload is not valid JSON") from exc
        if isinstance(value, dict) and value.get("id") == job_id and _complete(value):
            found.append(value)
    if not found:
        raise ValueError(f"zighang page has no complete recruitment payload for {job_id}")
    if any(value != found[0] for value in found[1:]):
        raise ValueError("zighang page has conflicting recruitment payloads")
    return found[0]


def _career(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("zighang career bound is not an integer")
    return value


# The current apply option decides alone when one exists: falling through to another
# candidate would substitute a posting zighang does not currently link to.
def _origin_candidates(recruitment: dict) -> tuple[str, ...]:
    dedup = _field(recruitment, "dedup")
    options = _field(dedup, "applyOptions") if isinstance(dedup, dict) else None
    options = [option for option in options if isinstance(option, dict)] if isinstance(options, list) else []
    current = [option for option in options if option.get("isCurrent") is True]
    if current:
        candidates = [_field(option, "redirectUrl") for option in current]
    else:
        candidates = [_field(option, "redirectUrl") for option in options if option.get("isRepresentative") is True]
        candidates.append(_field(recruitment, "redirectUrl"))
    return tuple(candidate for candidate in candidates if isinstance(candidate, str) and candidate)


def parse_zighang_page(page: str, job_id: str) -> ZighangPosting:
    if not ZIGHANG_ID_RE.fullmatch(job_id):
        raise ValueError("zighang job id is not a lowercase UUID")
    recruitment = _recruitment(_payload(page), job_id)
    title = _field(recruitment, "title")
    company = _field(recruitment, "company")
    company_name = _field(company, "name") if isinstance(company, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise ValueError("zighang recruitment has no title")
    if not isinstance(company_name, str) or not company_name.strip():
        raise ValueError("zighang recruitment has no company name")
    regions = _field(recruitment, "regions") or []
    if not isinstance(regions, list) or not all(isinstance(region, str) and not region.startswith("$") for region in regions):
        raise ValueError("zighang recruitment regions are not strings")
    experience = format_zighang_experience(_career(_field(recruitment, "careerMin")), _career(_field(recruitment, "careerMax")))
    # An unreadable document is rejected rather than read as image-only, and a posting
    # with no text in either document is never saved as an empty JD.
    content, summary = _doc(recruitment, "content"), _doc(recruitment, "summary")
    if content and tiptap_to_text(content):
        source_doc, used_summary = content, False
    elif summary and tiptap_to_text(summary):
        source_doc, used_summary = summary, True
    else:
        raise ValueError("zighang recruitment has no readable text")
    return ZighangPosting(
        job_id=job_id,
        title=title.strip(),
        company_name=company_name.strip(),
        experience=experience,
        location=", ".join(region.strip() for region in regions if region.strip()),
        used_summary=used_summary,
        origin_candidates=_origin_candidates(recruitment),
        **_sections(source_doc),
    )


_WANTED_HOST = "www.wanted.co.kr"
_REMEMBER_HOST = "career.rememberapp.co.kr"
_GROUPBY_HOST = "groupby.kr"
_SARAMIN_HOST = "www.saramin.co.kr"


# Origins resolve through the same exact-host, anchored-path lookup the extraction stage
# uses, and the URL is rebuilt from the id: the raw redirect URL is never passed on.
def _origin(url: str) -> str | None:
    posting = posting_key_from_url(url)
    if posting is None or urlparse(url).scheme != "https":
        return None
    platform, job_id = posting
    if platform == "wanted":
        return f"https://{_WANTED_HOST}/wd/{job_id}"
    if platform == "remember":
        return f"https://{_REMEMBER_HOST}/job/posting/{job_id}"
    if platform == "groupby":
        return f"https://{_GROUPBY_HOST}/positions/{job_id}"
    if platform == "saramin":
        return f"https://{_SARAMIN_HOST}/zf_user/jobs/relay/view?rec_idx={job_id}"
    return None


def supported_origin_url(posting: ZighangPosting) -> str | None:
    for candidate in posting.origin_candidates:
        origin = _origin(candidate)
        if origin:
            return origin
    return None
