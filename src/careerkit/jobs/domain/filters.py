from __future__ import annotations

from careerkit.jobs.domain.naming import normalize_company_name


def is_rejected_company_name(
    company: str,
    rejected_companies: set[str],
    config_excludes: list[str] | None = None,
) -> bool:
    normalized = normalize_company_name(company)
    if not normalized:
        return False
    if normalized in rejected_companies:
        return True
    if config_excludes:
        return normalized in {normalize_company_name(value) for value in config_excludes}
    return False
