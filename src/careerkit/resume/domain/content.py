from __future__ import annotations

from datetime import datetime
import re


def extract_overview(content: str) -> str:
    lines = content.split("\n")
    result: list[str] = []
    in_overview = False
    for line in lines:
        if line.startswith("# "):
            result.append(line)
        elif line.startswith("## Overview"):
            in_overview = True
        elif line.startswith("## ") and in_overview:
            break
        elif in_overview:
            result.append(line)
    return "\n".join(result).strip()

def extract_company_info(content: str) -> tuple[str, str, str]:
    name = period = role = ""
    for line in content.split("\n"):
        if line.startswith("# "):
            name = line[2:].strip()
        elif line.startswith("- Period:"):
            period = line.split(":", 1)[1].strip()
        elif line.startswith("- Role:"):
            role = line.split(":", 1)[1].strip()
    return name, period, role


def extract_company_info_full(content: str) -> dict[str, str]:
    info = {
        "name": "",
        "period": "",
        "role": "",
        "employment": "정규직",
        "position": "",
    }
    for line in content.split("\n"):
        if line.startswith("# "):
            info["name"] = line[2:].strip()
        elif line.startswith("- Period:"):
            info["period"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Role:"):
            info["role"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Employment:"):
            info["employment"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Position:"):
            info["position"] = line.split(":", 1)[1].strip()
    return info


def _period_separator_text(separator: str) -> str:
    stripped = separator.strip()
    return f" {stripped} " if stripped in {"-", "~"} else separator


def _split_period(period_str: str, separator: str) -> list[str]:
    stripped = separator.strip()
    pattern = r"\s+-\s+" if stripped == "-" else rf"\s*{re.escape(stripped)}\s*"
    return re.split(pattern, period_str.strip(), maxsplit=1)


def _parse_year_month(value: str, default_month: int) -> tuple[int, int]:
    parts = re.split(r"[.-]", value)
    return int(parts[0]), int(parts[1]) if len(parts) > 1 else default_month


def calculate_tenure(period_str: str, *, separator: str = "-", include_period: bool = True, error_value: str | None = None, now: datetime | None = None) -> str:
    fallback = period_str if error_value is None else error_value
    parts = _split_period(period_str, separator)
    if len(parts) != 2:
        return fallback
    start_str, end_str = parts[0].strip(), parts[1].strip()
    try:
        start_year, start_month = _parse_year_month(start_str, 1)
        if end_str in ("현재", "재직중"):
            end_date = now or datetime.now()
            end_year, end_month, end_label = end_date.year, end_date.month, "재직중"
        else:
            end_year, end_month = _parse_year_month(end_str, 12)
            end_label = end_str
        total_months = (end_year - start_year) * 12 + (end_month - start_month) + 1
        years, months = divmod(total_months, 12)
        if years > 0 and months > 0:
            tenure = f"{years}년 {months}개월"
        elif years > 0:
            tenure = f"{years}년"
        else:
            tenure = f"{months}개월"
        if not include_period:
            return tenure
        return f"{start_str}{_period_separator_text(separator)}{end_label} ({tenure})"
    except (ValueError, IndexError):
        return fallback


def extract_section(content: str, section_name: str) -> str:
    lines = content.split("\n")
    result: list[str] = []
    in_section = False
    section_level = 0
    for line in lines:
        if line.startswith("## ") or line.startswith("### "):
            current_level = 2 if line.startswith("## ") else 3
            section_title = line.lstrip("#").strip()
            if section_title == section_name:
                in_section = True
                section_level = current_level
                continue
            if in_section and current_level <= section_level:
                break
        elif in_section:
            result.append(line)
    return "\n".join(result).strip()
