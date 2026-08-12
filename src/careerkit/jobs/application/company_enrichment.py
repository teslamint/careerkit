from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from careerkit.jobs.application.company_info import CompanyData, CompanyInfoLookup, CompanyInfoService, validate_company


@dataclass(frozen=True)
class CompanyEnrichmentContext:
    platform: str
    item_id: str
    company_name: str
    company_id: str | None
    source_url: str | None
    facts: dict[str, object]
    fact_sources: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class CompanyInfoEnrichmentResult:
    status: str
    attempted: bool
    persisted: bool
    completeness: float | None
    warning_code: str | None
    file_path: Path | None


class CompanyEnrichmentService:
    def __init__(self, *, company_info: CompanyInfoService) -> None:
        self.company_info = company_info

    def enrich(
        self,
        context: CompanyEnrichmentContext,
        *,
        dry_run: bool = False,
        timeout: float = 1.0,
    ) -> CompanyInfoEnrichmentResult:
        lookup = self.company_info.inspect(context.company_name)
        if lookup.status == "ready":
            return _result_from_lookup(lookup, attempted=False, persisted=False)
        if lookup.status in {"invalid", "unsafe"}:
            return CompanyInfoEnrichmentResult(
                status="error",
                attempted=False,
                persisted=False,
                completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
                warning_code=None,
                file_path=lookup.file_path,
            )
        merged = _merge_company_data(
            lookup.validation.data if lookup.validation is not None else CompanyData(name=context.company_name),
            context,
        )
        markdown = _render_company_markdown(context.company_name, merged)
        if dry_run:
            validation = validate_company(merged, Path("<memory>"))
            dry_lookup = CompanyInfoLookup(
                status="ready" if validation.completeness_score >= 70 else "incomplete",
                file_path=lookup.file_path,
                validation=validation,
                digest=lookup.digest,
            )
            return _result_from_lookup(dry_lookup, attempted=True, persisted=False)
        persisted_lookup = self.company_info.apply_candidate(
            company_name=context.company_name,
            markdown=markdown,
            expected_digest=lookup.digest,
            timeout=timeout,
        )
        return _result_from_lookup(persisted_lookup, attempted=True, persisted=True)


_COMPANY_DATA_FIELDS = frozenset(CompanyData.__dataclass_fields__)


def _result_from_lookup(
    lookup: CompanyInfoLookup,
    *,
    attempted: bool,
    persisted: bool,
) -> CompanyInfoEnrichmentResult:
    if lookup.status == "ready":
        return CompanyInfoEnrichmentResult(
            status="ready",
            attempted=attempted,
            persisted=persisted,
            completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
            warning_code=None,
            file_path=lookup.file_path,
        )
    return CompanyInfoEnrichmentResult(
        status="warning",
        attempted=attempted,
        persisted=persisted,
        completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
        warning_code="missing" if lookup.status == "missing" else "below_threshold",
        file_path=lookup.file_path,
    )


def _merge_company_data(existing: CompanyData, context: CompanyEnrichmentContext) -> CompanyData:
    values = existing.__dict__.copy()
    values["name"] = existing.name or context.company_name
    for field, candidate in context.facts.items():
        if field not in _COMPANY_DATA_FIELDS:
            continue
        if candidate in (None, "", ()):
            continue
        field_sources = tuple(url for url in context.fact_sources.get(field, ()) if url)
        if not field_sources:
            continue
        current = values.get(field)
        if current in (None, "", ()):
            values[field] = candidate
    sources = list(existing.sources)
    if context.source_url:
        sources.append(context.source_url)
    for field, urls in context.fact_sources.items():
        if field not in _COMPANY_DATA_FIELDS:
            continue
        if context.facts.get(field) in (None, "", ()):
            continue
        if values.get(field) != context.facts.get(field):
            continue
        sources.extend(url for url in urls if url)
    values["sources"] = tuple(dict.fromkeys(url for url in sources if url))
    return CompanyData(**values)


def _render_company_markdown(company_name: str, data: CompanyData) -> str:
    lines = [
        f"# {company_name}",
        "",
        "## 기업 정보",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
    ]
    if data.industry:
        lines.append(f"| 업종 | {data.industry} |")
    lines.append(f"| 스타트업 여부 | {'yes' if data.is_startup else 'no'} |")
    if data.founded_year is not None:
        lines.append(f"| 설립 | {data.founded_year}년 |")
    if data.employee_current is not None:
        lines.append(f"| 직원수 | {data.employee_current:,}명 |")
    if data.avg_salary is not None:
        lines.extend(["", "## 연봉 정보", "", "| 항목 | 금액 |", "|------|------|"])
        lines.append(f"| 평균 연봉 | **{data.avg_salary:,}만원** |")
    if data.revenue is not None:
        lines.extend(["", "## 매출 정보", "", "| 항목 | 내용 |", "|------|------|"])
        lines.append(f"| 매출액 | {data.revenue:g}억원 |")
    if data.employee_joined_1y is not None or data.employee_left_1y is not None or data.employee_mom_change is not None:
        lines.extend(["", "## 인원 통계", "", "| 항목 | 내용 |", "|------|------|"])
        if data.employee_current is not None:
            lines.append(f"| 현재 인원 | {data.employee_current:,}명 |")
        if data.employee_joined_1y is not None:
            lines.append(f"| 1년간 입사자 | {data.employee_joined_1y:,}명 |")
        if data.employee_left_1y is not None:
            lines.append(f"| 1년간 퇴사자 | {data.employee_left_1y:,}명 |")
        if data.employee_mom_change is not None:
            lines.append(f"| MoM | {data.employee_mom_change:.1f}% |")
    if data.investment_round or data.investment_total is not None:
        lines.extend(["", "## 투자 정보", "", "| 항목 | 내용 |", "|------|------|"])
        if data.investment_round:
            lines.append(f"| 현재 라운드 | {data.investment_round} |")
        if data.investment_total is not None:
            lines.append(f"| 누적 투자금 | 약 {data.investment_total:g}억원 |")
    lines.extend(["", "---", "", "*출처:*"])
    for url in data.sources:
        lines.append(f"- {url}")
    lines.append("")
    return "\n".join(lines)
