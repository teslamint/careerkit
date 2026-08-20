from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


SCHEMAS = {
    "contact": {
        "required_fields": ("Name", "Email"),
        "patterns": {"Email": r"[\w.-]+@[\w.-]+\.\w+"},
    },
    "profile": {
        "required_fields": ("Period", "Role"),
        "patterns": {"Period": r"\d{4}\.\d{2}\s*-\s*(\d{4}\.\d{2}|현재)"},
    },
    "project": {
        "required_sections": ("Tech Stack",),
        "min_items": {"Tech Stack": 1},
    },
}


VARIANT_TAGS = [
    ("<!-- job-only:start -->", "<!-- job-only:end -->"),
    ("<!-- public-only:start -->", "<!-- public-only:end -->"),
    ("<!-- common:start -->", "<!-- common:end -->"),
]


@dataclass(frozen=True)
class ValidationError:
    file_path: str
    message: str
    line: int | None = None


def extract_field(content: str, field: str) -> str | None:
    match = re.search(rf"^-\s*{re.escape(field)}:\s*(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else None


def extract_section_items(content: str, section: str) -> list[str]:
    items: list[str] = []
    in_section = False
    for line in content.splitlines():
        if line.startswith(("## ", "### ")):
            current_section = line.lstrip("#").strip()
            if current_section == section:
                in_section = True
                continue
            if in_section:
                break
        elif in_section and line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _validate_fields(content: str, file_path: str, schema_name: str) -> list[ValidationError]:
    schema = SCHEMAS[schema_name]
    patterns = schema.get("patterns", {})
    errors: list[ValidationError] = []
    for field in schema["required_fields"]:
        value = extract_field(content, field)
        if not value:
            errors.append(ValidationError(file_path, f"Missing required field: {field}"))
        elif field in patterns and not re.search(patterns[field], value):
            pattern = patterns[field]
            errors.append(
                ValidationError(
                    file_path,
                    f"Invalid {field} format: '{value}' (expected pattern: {pattern})",
                )
            )
    return errors


def validate_project(content: str, file_path: str) -> list[ValidationError]:
    schema = SCHEMAS["project"]
    errors: list[ValidationError] = []
    for section in schema["required_sections"]:
        item_count = len(extract_section_items(content, section))
        minimum = schema["min_items"].get(section, 0)
        if item_count < minimum:
            errors.append(
                ValidationError(
                    file_path,
                    f"Section '{section}' requires at least {minimum} item(s), found {item_count}",
                )
            )
    return errors


def validate_variant_tags(content: str, file_path: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    lines = content.split("\n")
    for start_tag, end_tag in VARIANT_TAGS:
        tag_name = start_tag.replace("<!-- ", "").replace(":start -->", "")
        open_stack: list[int] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == start_tag:
                open_stack.append(i)
            elif stripped == end_tag:
                if not open_stack:
                    errors.append(ValidationError(file_path, f"'{end_tag}' without matching start tag", i))
                else:
                    open_stack.pop()
        for line_num in open_stack:
            errors.append(ValidationError(file_path, f"Unclosed '{tag_name}' block", line_num))
    return errors


def validate_file(file_path: Path) -> list[ValidationError]:
    if not file_path.exists():
        return [ValidationError(str(file_path), "File not found")]
    content = file_path.read_text(encoding="utf-8")
    path_str = str(file_path)
    errors = validate_variant_tags(content, path_str)
    if file_path.name == "contact.md":
        errors.extend(_validate_fields(content, path_str, "contact"))
    elif file_path.name == "profile.md" and "companies" in file_path.parts:
        errors.extend(_validate_fields(content, path_str, "profile"))
    elif "projects" in file_path.parts:
        errors.extend(validate_project(content, path_str))
    return errors


def validate_company_key_case(base_dir: Path) -> list[ValidationError]:
    companies_dir = base_dir / "companies"
    company_names = {path.name for path in companies_dir.iterdir() if path.is_dir()} if companies_dir.is_dir() else set()
    folded_names = {name.casefold(): name for name in sorted(company_names)}
    errors: list[ValidationError] = []
    config_paths = [base_dir / "variant_config.json", *sorted((base_dir / "overrides").glob("*/config.json"))]
    for config_path in config_paths:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(config, dict):
            continue
        configured_keys: set[str] = set()
        for variant_config in config.values():
            if not isinstance(variant_config, dict):
                continue
            companies = variant_config.get("companies", [])
            if isinstance(companies, list):
                configured_keys.update(company for company in companies if isinstance(company, str))
            company_detail = variant_config.get("company_detail", {})
            if company_detail is not None and not isinstance(company_detail, dict):
                errors.append(
                    ValidationError(
                        str(config_path),
                        f"company_detail must be a dict, got {type(company_detail).__name__}",
                    )
                )
            if isinstance(company_detail, dict):
                for company, value in company_detail.items():
                    if not isinstance(company, str):
                        continue
                    configured_keys.add(company)
                    if isinstance(value, dict):
                        unknown_keys = set(value.keys()) - {"level", "projects", "achievements", "exclude_projects"}
                        if unknown_keys:
                            errors.append(
                                ValidationError(
                                    str(config_path),
                                    f"company_detail['{company}'] has unknown keys: {sorted(unknown_keys)}",
                                )
                            )
                        level = value.get("level")
                        if level is not None and level not in ("full", "summary"):
                            errors.append(
                                ValidationError(
                                    str(config_path),
                                    f"company_detail['{company}'].level must be 'full' or 'summary', got {level!r}",
                                )
                            )
                        for list_key in ("projects", "achievements", "exclude_projects"):
                            list_val = value.get(list_key)
                            if list_val is None:
                                continue
                            if not isinstance(list_val, list) or not all(isinstance(item, str) for item in list_val):
                                errors.append(
                                    ValidationError(
                                        str(config_path),
                                        f"company_detail['{company}'].{list_key} must be a list of strings or null,"
                                        f" got {type(list_val).__name__}",
                                    )
                                )
                    elif isinstance(value, str):
                        if value not in ("full", "summary"):
                            errors.append(
                                ValidationError(
                                    str(config_path),
                                    f"company_detail['{company}'] must be 'full' or 'summary', got {value!r}",
                                )
                            )
                    else:
                        errors.append(
                            ValidationError(
                                str(config_path),
                                f"company_detail['{company}'] must be a string or dict, got {type(value).__name__}",
                            )
                        )
        for company in sorted(configured_keys):
            if company in company_names:
                continue
            expected = folded_names.get(company.casefold())
            if expected:
                errors.append(
                    ValidationError(
                        str(config_path),
                        f"Company key case mismatch: '{company}' must match directory '{expected}'",
                    )
                )
            else:
                errors.append(
                    ValidationError(
                        str(config_path),
                        f"Company key has no matching directory: '{company}'",
                    )
                )
    return errors


def validate_all(base_dir: Path) -> list[ValidationError]:
    errors = validate_company_key_case(base_dir)
    for path in sorted(base_dir.rglob("*.md")):
        if path.name == "CLAUDE.md":
            continue
        errors.extend(validate_file(path))
    return errors
