from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

METRIC_PATTERN = re.compile(r"\d+%\s*(?:이상\s*|p\s*)?(?:단축|감소|개선|향상|절감|증가)")


@dataclass
class VerifierConfig:
    company_aliases: dict[str, list[str]]
    parent_map: dict[str, str]
    keywords: list[str]
    metric_pattern: re.Pattern[str]

    @property
    def child_map(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for child, parent in self.parent_map.items():
            result.setdefault(parent, []).append(child)
        return result

    @property
    def alias_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, aliases in self.company_aliases.items():
            for alias in aliases:
                result[alias] = key
                result[alias.lower()] = key
        return result


def _keyword_list(data: dict[object, object], field_name: str) -> list[str]:
    values = data.get(field_name)
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise ValueError(f"verify_content_config.json {field_name} must be a string list")
    return values


def verifier_config_from_data(data: object) -> VerifierConfig:
    if not isinstance(data, dict):
        raise ValueError("verify_content_config.json must contain a JSON object")

    raw_aliases = data.get("company_aliases")
    raw_parent_map = data.get("parent_company_map", {})
    technology_keywords = _keyword_list(data, "technology_keywords")
    pattern_keywords = _keyword_list(data, "pattern_keywords")
    if not isinstance(raw_aliases, dict) or not raw_aliases:
        raise ValueError("verify_content_config.json company_aliases must be a non-empty object")
    if not isinstance(raw_parent_map, dict):
        raise ValueError("verify_content_config.json parent_company_map must be an object")
    if not technology_keywords and not pattern_keywords:
        raise ValueError("verify_content_config.json must declare at least one verification keyword")

    company_aliases: dict[str, list[str]] = {}
    for company, aliases in raw_aliases.items():
        if not isinstance(company, str) or not company.strip():
            raise ValueError("verify_content_config.json company keys must be non-empty strings")
        if not isinstance(aliases, list) or not aliases or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise ValueError(
                f"verify_content_config.json aliases for {company!r} must be a non-empty string list"
            )
        company_aliases[company] = list(aliases)

    parent_map: dict[str, str] = {}
    for child, parent in raw_parent_map.items():
        if not isinstance(child, str) or not isinstance(parent, str):
            raise ValueError("verify_content_config.json parent mappings must use string keys and values")
        if child not in company_aliases or parent not in company_aliases:
            raise ValueError(
                "verify_content_config.json parent mappings must reference declared company_aliases"
            )
        parent_map[child] = parent

    return VerifierConfig(
        company_aliases=company_aliases,
        parent_map=parent_map,
        keywords=list(dict.fromkeys([*technology_keywords, *pattern_keywords])),
        metric_pattern=METRIC_PATTERN,
    )


@dataclass
class Claim:
    company_key: str
    keyword: str
    line_number: int


@dataclass
class VerificationResult:
    claim: Claim
    status: str
    evidence_line: Optional[int] = None


def _is_ascii(value: str) -> bool:
    return all(ord(character) < 128 for character in value)


def _alias_match(alias: str, text: str) -> Optional[int]:
    if _is_ascii(alias):
        match = re.search(r"(?<![a-zA-Z0-9])" + re.escape(alias) + r"(?![a-zA-Z0-9])", text, re.IGNORECASE)
    else:
        match = re.search(re.escape(alias), text)
    return match.start() if match else None


def parse_resume_sections(resume_path: Path, config: VerifierConfig) -> dict[str, str]:
    content = resume_path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    sorted_aliases = sorted(config.alias_map.items(), key=lambda item: len(item[0]), reverse=True)
    for part in re.split(r"^# ", content, flags=re.MULTILINE)[1:]:
        first_line = part.split("\n", 1)[0].strip()
        matched_key = None
        for alias, key in sorted_aliases:
            if _alias_match(alias, first_line) is not None:
                matched_key = key
                break
        if matched_key:
            section_text = f"# {part}"
            sections[matched_key] = section_text
            for alias in config.company_aliases.get(matched_key, []):
                sections[alias] = section_text
    return sections


def extract_claims(interview_path: Path, config: VerifierConfig) -> list[Claim]:
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(interview_path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.startswith("> "):
            continue
        text = line[2:]
        mentioned: dict[str, int] = {}
        for alias, key in config.alias_map.items():
            pos = _alias_match(alias, text)
            if pos is not None and (key not in mentioned or pos < mentioned[key]):
                mentioned[key] = pos
        if not mentioned:
            continue
        company_list = sorted(mentioned.items(), key=lambda item: item[1])
        text_lower = text.lower()
        for keyword in config.keywords:
            kw_pos = text_lower.find(keyword.lower())
            if kw_pos == -1:
                continue
            closest_company = min(company_list, key=lambda item: abs(item[1] - kw_pos))
            claim_id = (closest_company[0], keyword.lower())
            if claim_id not in seen:
                seen.add(claim_id)
                claims.append(Claim(closest_company[0], keyword, line_number))
        for match in config.metric_pattern.finditer(text):
            metric = match.group()
            closest_company = min(company_list, key=lambda item: abs(item[1] - match.start()))
            claim_id = (closest_company[0], metric)
            if claim_id not in seen:
                seen.add(claim_id)
                claims.append(Claim(closest_company[0], metric, line_number))
    return claims


def verify_claims(claims: list[Claim], sections: dict[str, str], config: VerifierConfig) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    child_map = config.child_map
    for claim in claims:
        company_section = sections.get(claim.company_key, "")
        parent_key = config.parent_map.get(claim.company_key)
        parent_section = sections.get(parent_key, "") if parent_key else ""
        child_keys = child_map.get(claim.company_key, [])
        child_sections = "\n".join(sections.get(child_key, "") for child_key in child_keys)
        combined = company_section + "\n" + parent_section + "\n" + child_sections
        keyword_lower = claim.keyword.lower()
        if combined.strip() and keyword_lower in combined.lower():
            search_section = company_section if keyword_lower in company_section.lower() else parent_section
            if not search_section:
                for child_key in child_keys:
                    child_section = sections.get(child_key, "")
                    if keyword_lower in child_section.lower():
                        search_section = child_section
                        break
            evidence_line = None
            if search_section:
                for index, search_line in enumerate(search_section.split("\n"), 1):
                    if keyword_lower in search_line.lower():
                        evidence_line = index
                        break
            results.append(VerificationResult(claim, "verified", evidence_line=evidence_line))
            continue
        skip_keys = {claim.company_key} | set(config.company_aliases.get(claim.company_key, []))
        found_elsewhere = None
        found_line = None
        for key, section_text in sections.items():
            if key in skip_keys or key not in config.company_aliases:
                continue
            if keyword_lower in section_text.lower():
                found_elsewhere = key
                for index, search_line in enumerate(section_text.split("\n"), 1):
                    if keyword_lower in search_line.lower():
                        found_line = index
                        break
                break
        if found_elsewhere:
            results.append(VerificationResult(claim, "uncertain", evidence_line=found_line))
        else:
            results.append(VerificationResult(claim, "ungrounded"))
    return results
