from __future__ import annotations

from pathlib import Path
from typing import Any

from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from datetime import datetime

from careerkit.resume.domain.content import calculate_tenure, extract_company_info_full, extract_section
from careerkit.resume.domain.variants import filter_content

CONTACT_LABELS = {
    "Name": "이름",
    "Email": "이메일",
    "Phone": "연락처",
    "GitHub": "GitHub",
    "LinkedIn": "LinkedIn",
}
COMPANY_METADATA = {"Period", "Role", "Employment", "Position", "Department", "부서"}
PROJECT_METADATA = {"Period", "Type", "Tech Stack"}


def _filtered_file(adapter: ResumeWorkspaceAdapter, path: Path, variant: str) -> str:
    return filter_content(adapter.read_file(path), variant)


def _without_metadata(content: str, fields: set[str]) -> str:
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            key = stripped[2:].split(":", 1)[0].strip()
            if key in fields:
                continue
        if stripped:
            lines.append(stripped)
    return " ".join(lines)


def _title(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def build_contact(adapter: ResumeWorkspaceAdapter, variant: str) -> str:
    contact_path = adapter.profile_dir / "contact.md"
    if not contact_path.exists():
        return ""
    parts = ["## 인적사항"]
    for line in _filtered_file(adapter, contact_path, variant).splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") or not stripped:
            continue
        if stripped.startswith("- "):
            key, _, value = stripped[2:].partition(":")
            parts.append(f"- {CONTACT_LABELS.get(key.strip(), key.strip())}: {value.strip()}")
    return "\n".join(parts) if len(parts) > 1 else ""


def build_career_project(
    adapter: ResumeWorkspaceAdapter,
    project_path: Path,
    index: int,
    variant: str,
) -> str:
    content = _filtered_file(adapter, project_path, variant)
    title = _title(content)
    summary = _without_metadata(extract_section(content, "Summary"), PROJECT_METADATA)
    overview = _without_metadata(extract_section(content, "Overview"), PROJECT_METADATA)
    legacy_period = " ".join(extract_section(content, "Period").splitlines()).strip()
    responsibilities = extract_section(content, "Key Responsibilities") or extract_section(content, "Responsibilities")
    achievements = extract_section(content, "Achievements")
    if not title or not any((summary, overview, legacy_period, responsibilities, achievements)):
        return ""

    parts = [f"### 프로젝트 {index}: {title}"]
    context = summary or overview
    if context:
        parts.append(f"- 개요: {context}")
    elif legacy_period:
        parts.append(f"- 기간: {legacy_period}")
    if responsibilities.strip():
        parts.append("- 상세 업무:")
        parts.extend(f"  {line}" for line in responsibilities.splitlines() if line.strip())
    if achievements.strip():
        parts.append("- 성과:")
        parts.extend(f"  {line}" for line in achievements.splitlines() if line.strip())
    return "\n".join(parts)


def build_career_company(
    adapter: ResumeWorkspaceAdapter,
    company_dir: Path,
    variant: str,
    detail_level: str,
    *,
    now: datetime | None = None,
) -> str:
    profile = company_dir / "profile.md"
    if not profile.exists():
        return ""
    profile_content = _filtered_file(adapter, profile, variant)
    info = extract_company_info_full(profile_content)
    parts = [f"## {info['name']}"]
    if info["period"]:
        parts.append(f"- 기간: {calculate_tenure(info['period'], now=now)}")
    if info["role"]:
        parts.append(f"- 역할: {info['role']}")
    if info["employment"] and any(line.strip().startswith("- Employment:") for line in profile_content.splitlines()):
        parts.append(f"- 고용형태: {info['employment']}")
    if info["position"]:
        parts.append(f"- 직급: {info['position']}")
    for line in extract_section(profile_content, "Overview").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- Department:", "- 부서:")):
            parts.append(f"- 부서: {stripped.split(':', 1)[1].strip()}")
            break

    summary = _without_metadata(extract_section(profile_content, "Summary"), COMPANY_METADATA)
    overview = _without_metadata(extract_section(profile_content, "Overview"), COMPANY_METADATA)
    narrative = summary or overview
    if narrative:
        parts.append(f"- 담당업무: {narrative}")

    detail = _normalize_company_detail(detail_level)
    if detail["level"] == "full":
        excluded = set(detail.get("exclude_projects") or [])
        allowed = detail.get("projects")
        project_index = 1
        for path_str in adapter.resolve_glob(company_dir / "projects", "*.md"):
            path = Path(path_str)
            if path.name == "CLAUDE.md":
                continue
            if path.stem in excluded or path.name in excluded:
                continue
            if allowed is not None and path.stem not in allowed and path.name not in allowed:
                continue
            project = build_career_project(adapter, path, project_index, variant)
            if not project:
                continue
            parts.extend(("", project))
            project_index += 1
    return "\n".join(parts)


def _normalize_company_detail(raw: Any) -> dict[str, Any]:
    """Normalize company_detail entry to {level, projects, achievements, exclude_projects}."""
    if isinstance(raw, str):
        return {"level": raw, "projects": None, "achievements": None, "exclude_projects": None}
    if not isinstance(raw, dict):
        return {"level": "full", "projects": None, "achievements": None, "exclude_projects": None}
    return {
        "level": raw.get("level", "full"),
        "projects": raw.get("projects"),
        "achievements": raw.get("achievements"),
        "exclude_projects": raw.get("exclude_projects"),
    }


def build_career(
    adapter: ResumeWorkspaceAdapter,
    variant: str,
    format_type: str = "md",
    *,
    now: datetime | None = None,
) -> str:
    parts = ["# 경력기술서"]
    contact = build_contact(adapter, variant)
    if contact:
        parts.append(contact)

    config = adapter.load_target_config(adapter.target, variant)
    companies: list[str] = []
    for company in config.get("companies", []):
        company_dir = adapter.companies_dir / company
        detail_level = config.get("company_detail", {}).get(company, "full")
        rendered = build_career_company(adapter, company_dir, variant, detail_level, now=now)
        if rendered:
            companies.append(rendered)
    if not companies:
        parts.append("\n(등록된 경력 없음)")
        return "\n\n".join(parts)

    parts.extend(companies)
    separator = "\n\n" if format_type == "pdf" else "\n\n---\n\n"
    return separator.join(parts)
