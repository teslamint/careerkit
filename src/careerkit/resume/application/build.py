from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from datetime import datetime

from careerkit.resume.domain.content import calculate_tenure, extract_company_info, extract_company_info_full, extract_overview, extract_section
from careerkit.resume.domain.variants import filter_content


class ResumeBuildService:
    def __init__(self, adapter: ResumeWorkspaceAdapter) -> None:
        self.adapter = adapter

    def build_profile(self, variant: str) -> list[str]:
        config = self.adapter.load_target_config(self.adapter.target, variant)
        parts: list[str] = []
        profile_dir = self.adapter.profile_dir
        for path in (
            profile_dir / "contact.md",
            profile_dir / f"summary-{variant}.md",
            profile_dir / f"skills-{variant}.md",
            profile_dir / "education.md",
        ):
            if path.exists():
                parts.append(self.adapter.read_file(path))
        if config.get("include_open_source", True):
            path = profile_dir / "open-source.md"
            if path.exists():
                parts.append(self.adapter.read_file(path))
        if config.get("include_awards", True):
            path = profile_dir / "awards.md"
            if path.exists():
                parts.append(self.adapter.read_file(path))
        if config.get("include_languages", True):
            path = profile_dir / "languages.md"
            if path.exists():
                parts.append(self.adapter.read_file(path))
        return parts

    def build_profile_short(self, variant: str) -> list[str]:
        parts: list[str] = []
        profile_dir = self.adapter.profile_dir
        for path in (
            profile_dir / "contact.md",
            profile_dir / f"summary-{variant}.md",
            profile_dir / f"skills-{variant}.md",
        ):
            if path.exists():
                content = self.adapter.read_file(path)
                if path.name.startswith("skills-"):
                    techs = [line[2:].strip() for line in content.split("\n") if line.startswith("- ")]
                    if techs:
                        parts.append("# Skills\n\n" + " | ".join(techs))
                else:
                    parts.append(content)
        return parts

    def build_company(self, company_dir: Path, variant: str) -> list[str]:
        config = self.adapter.load_target_config(self.adapter.target, variant)
        detail_level = config.get("company_detail", {}).get(company_dir.name, "full")
        profile = company_dir / "profile.md"
        if not profile.exists():
            return []
        profile_content = filter_content(self.adapter.read_file(profile), variant)
        if detail_level == "summary":
            return [extract_overview(profile_content)]
        parts = [profile_content.strip()]
        for rel_dir in ("projects", "achievements"):
            base_dir = company_dir / rel_dir
            if base_dir.exists():
                for path_str in self.adapter.resolve_glob(base_dir, "*.md"):
                    path = Path(path_str)
                    if path.name == "CLAUDE.md":
                        continue
                    parts.append(filter_content(self.adapter.read_file(path), variant).strip())
        return [part for part in parts if part]

    def build_full(self, variant: str) -> str:
        config = self.adapter.load_target_config(self.adapter.target, variant)
        parts = self.build_profile(variant)
        parts.append("# Experience")
        for company in config["companies"]:
            company_dir = self.adapter.companies_dir / company
            if company_dir.exists():
                parts.extend(self.build_company(company_dir, variant))
        return "\n\n---\n\n".join(parts)

    def build_full_pdf(self, variant: str) -> str:
        if variant != "job":
            return self.build_full(variant)
        config = self.adapter.load_target_config(self.adapter.target, variant)
        parts: list[str] = []
        for path in (
            self.adapter.profile_dir / f"summary-{variant}.md",
            self.adapter.profile_dir / f"skills-{variant}.md",
        ):
            if path.exists():
                content = filter_content(self.adapter.read_file(path), variant).strip()
                if content:
                    parts.append(content)
        parts.append("# Experience")
        for company in config["companies"]:
            company_dir = self.adapter.companies_dir / company
            if company_dir.exists():
                parts.extend(self.build_company(company_dir, variant))
        optional_sections = (
            ("include_open_source", "open-source.md"),
            (None, "education.md"),
            ("include_languages", "languages.md"),
            ("include_awards", "awards.md"),
        )
        for config_key, filename in optional_sections:
            if config_key is not None and not config.get(config_key, True):
                continue
            path = self.adapter.profile_dir / filename
            if path.exists():
                content = filter_content(self.adapter.read_file(path), variant).strip()
                if content:
                    parts.append(content)
        contact_links = self.extract_contact_links(variant)
        if contact_links:
            parts.append(contact_links)
        return "\n\n".join(parts)

    def extract_contact_links(self, variant: str) -> str | None:
        contact = self.adapter.profile_dir / "contact.md"
        if not contact.exists():
            return None
        content = filter_content(self.adapter.read_file(contact), variant)
        lines = [line for line in content.splitlines() if line.strip() and not line.strip().startswith("#") and not line.strip().lower().startswith("- name:")]
        return None if not lines else "# Links\n\n" + "\n".join(lines)

    def build_short(self, variant: str) -> str:
        config = self.adapter.load_target_config(self.adapter.target, variant)
        parts = self.build_profile_short(variant)
        parts.append("# Experience\n")
        table = ["| 회사 | 기간 | 역할 |", "|------|------|------|"]
        for company in config["companies"]:
            profile = self.adapter.companies_dir / company / "profile.md"
            if profile.exists():
                name, period, role = extract_company_info(filter_content(self.adapter.read_file(profile), variant))
                table.append(f"| {name} | {period} | {role} |")
        parts.append("\n".join(table))
        education_summary = self._build_education_summary(variant)
        if education_summary:
            parts.append(education_summary)
        return "\n\n".join(parts)

    def build_short_pdf(self, variant: str) -> str:
        if variant != "job":
            return self.build_short(variant)
        config = self.adapter.load_target_config(self.adapter.target, "job")
        parts: list[str] = []
        summary = self.adapter.profile_dir / f"summary-{variant}.md"
        if summary.exists():
            parts.append(filter_content(self.adapter.read_file(summary), variant))
        contact_links = self.extract_contact_links(variant)
        if contact_links:
            parts.append(contact_links)
        skills = self.adapter.profile_dir / f"skills-{variant}.md"
        if skills.exists():
            techs: list[str] = []
            for line in filter_content(self.adapter.read_file(skills), variant).split("\n"):
                if line.startswith("- "):
                    tech = line[2:].strip()
                    if tech:
                        techs.append(tech)
            if techs:
                parts.append(f"# Skills\n{', '.join(techs)}")
        table = ["# Experience\n", "| 회사 | 기간 | 역할 |", "|------|------|------|"]
        for company in config["companies"]:
            profile = self.adapter.companies_dir / company / "profile.md"
            if profile.exists():
                name, period, role = extract_company_info(filter_content(self.adapter.read_file(profile), variant))
                table.append(f"| {name} | {period} | {role} |")
        parts.append("\n".join(table))
        education_summary = self._build_education_summary(variant)
        if education_summary:
            parts.append(education_summary)
        return "\n\n".join(parts)

    def _build_education_summary(self, variant: str) -> str | None:
        education = self.adapter.profile_dir / "education.md"
        if not education.exists():
            return None
        school = period = major = ""
        content = filter_content(self.adapter.read_file(education), variant)
        for line in content.splitlines():
            if line.startswith("## "):
                school = line[3:].strip()
            elif line.startswith("- Period:"):
                period = line.split(":", 1)[1].strip()
            elif line.startswith("- Major:"):
                major = line.split(":", 1)[1].strip()
        return f"# Education\n{school} | {major} ({period})"

    def build_wanted(self, variant: str, *, now: datetime | None = None) -> str:
        config_all = self.adapter.load_variant_config()
        config = config_all.get(variant, config_all["job"])
        profile_dir = self.adapter.profile_dir
        lines: list[str] = []
        contact = profile_dir / "contact.md"
        name = phone = email = github = ""
        if contact.exists():
            for line in self.adapter.read_file(contact).split("\n"):
                if line.startswith("- Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("- Phone:"):
                    phone = line.split(":", 1)[1].strip()
                elif line.startswith("- Email:"):
                    email = line.split(":", 1)[1].strip()
                elif line.startswith("- GitHub:"):
                    github = line.split(":", 1)[1].strip()
        lines.extend([name, f"📞 {phone}  @ {email}", ""])
        summary = profile_dir / f"summary-{variant}.md"
        if summary.exists():
            content = filter_content(self.adapter.read_file(summary), variant)
            for line in content.split("\n"):
                if line.startswith("# ") or line.startswith("## "):
                    continue
                if line.strip():
                    lines.append(line.strip())
            lines.append("")
        lines.extend(["경력", ""])
        for company_name in config["companies"]:
            company_dir = self.adapter.companies_dir / company_name
            profile = company_dir / "profile.md"
            if not profile.exists():
                continue
            content = filter_content(self.adapter.read_file(profile), variant)
            info = extract_company_info_full(content)
            lines.append(info["name"])
            lines.append(f"{calculate_tenure(info['period'], now=now)} | {info['employment']} | {info['role']}" + (f" | {info['position']}" if info["position"] else ""))
            lines.append("")
            summary_text = extract_section(content, "Summary")
            if summary_text:
                lines.extend(line.strip() for line in summary_text.splitlines() if line.strip() and not line.startswith("**"))
                lines.append("")
            overview_text = extract_section(content, "Overview")
            inline_summary = [
                line.strip() for line in overview_text.splitlines()
                if line.strip() and not line.strip().startswith("- ") and not line.startswith("**")
            ]
            if inline_summary and not summary_text:
                lines.extend(inline_summary)
                lines.append("")
            responsibilities = extract_section(content, "Key Responsibilities")
            if responsibilities:
                lines.extend(line.replace("**", "").strip() for line in responsibilities.splitlines() if line.strip())
                lines.append("")
            key_experience: list[str] = []
            in_key_experience = False
            for line in overview_text.splitlines():
                if "**Key Experience**" in line:
                    in_key_experience = True
                    continue
                if in_key_experience and line.strip():
                    key_experience.append(line)
            if key_experience:
                lines.extend(["Key Experience", *key_experience, ""])
            if config.get("company_detail", {}).get(company_name, "full") == "full":
                for path_str in self.adapter.resolve_glob(company_dir / "projects", "*.md"):
                    path = Path(path_str)
                    if path.name == "CLAUDE.md":
                        continue
                    project = _extract_wanted_project(filter_content(self.adapter.read_file(path), variant))
                    if not project.title:
                        continue
                    lines.append(project.title)
                    if project.period:
                        lines.append(project.period)
                    if project.tech_stack:
                        lines.append(f"기술스택: {', '.join(project.tech_stack)}")
                    if project.responsibilities:
                        lines.append(" ".join(project.responsibilities))
                    for achievement in project.achievements:
                        clean = achievement.replace("**", "")
                        lines.append(clean if achievement.startswith("**") else f"- {clean}")
                    lines.append("")
            lines.append("")
        education = profile_dir / "education.md"
        lines.extend(["학력", ""])
        if education.exists():
            education_content = self.adapter.read_file(education)
            fields = _metadata_fields(education_content)
            lines.append(fields.get("title", ""))
            structured = [fields.get("Period", ""), fields.get("Status", ""), fields.get("Major", "")]
            if any(structured):
                lines.append(" | ".join(structured))
            else:
                lines.extend(_plain_markdown_items(education_content))
        lines.append("")
        lines.extend(["스킬", ""])
        skills = profile_dir / f"skills-{variant}.md"
        if skills.exists():
            techs = [re.sub(r"\s*\([^)]*\)", "", line[2:].strip()) for line in self.adapter.read_file(skills).splitlines() if line.startswith("- ")]
            lines.append(" | ".join(tech for tech in techs if tech))
        lines.append("")
        awards = profile_dir / "awards.md"
        if awards.exists():
            awards_content = self.adapter.read_file(awards)
            fields = _metadata_fields(awards_content)
            lines.extend(["수상/자격증/기타", ""])
            if fields.get("Period") or fields.get("Description"):
                lines.append(fields.get("title", ""))
            if fields.get("Period"):
                lines.append(fields["Period"])
            if fields.get("Description"):
                lines.append(fields["Description"])
            if not fields.get("Period") and not fields.get("Description"):
                lines.extend(_plain_markdown_items(awards_content, include_subheadings=True))
            lines.append("")
        languages = profile_dir / "languages.md"
        if languages.exists():
            lines.extend(["언어", ""])
            lines.extend(line[2:].strip() for line in self.adapter.read_file(languages).splitlines() if line.startswith("- "))
            lines.append("")
        if github:
            lines.extend(["링크", "", f"GitHub: {github}", ""])
        return "\n".join(lines)


def _metadata_fields(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in content.splitlines():
        if line.startswith("## "):
            result["title"] = line[3:].strip()
        elif line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            result[key.strip()] = value.strip()
    return result


def _plain_markdown_items(content: str, *, include_subheadings: bool = False) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        if include_subheadings and line.startswith("## "):
            items.append(line[3:].strip())
        elif line.startswith("- "):
            items.append(line[2:].strip())
    return items


@dataclass(frozen=True)
class _WantedProject:
    title: str
    period: str
    tech_stack: list[str]
    responsibilities: list[str]
    achievements: list[str]


def _extract_wanted_project(content: str) -> _WantedProject:
    title = next((line.lstrip("#").strip() for line in content.splitlines() if line.startswith("#")), "")
    overview = extract_section(content, "Overview")
    period = next((line.split(":", 1)[1].strip() for line in overview.splitlines() if line.strip().startswith("- Period:")), "")
    if not period:
        period = extract_section(content, "Period").strip()
    return _WantedProject(
        title=title,
        period=period,
        tech_stack=[line[2:].strip() for line in extract_section(content, "Tech Stack").splitlines() if line.strip().startswith("- ")],
        responsibilities=[line[2:].strip() if line.strip().startswith("- ") else line.strip() for line in (extract_section(content, "Key Responsibilities") or extract_section(content, "Responsibilities")).splitlines() if line.strip()],
        achievements=[line[2:].strip() if line.strip().startswith("- ") else line.strip() for line in extract_section(content, "Achievements").splitlines() if line.strip()],
    )
