from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import os
from pathlib import Path
import tempfile
import time
import re
from typing import Callable, Optional

import fcntl

from careerkit.jobs.domain.naming import normalize_company_name, slugify_company
from careerkit.workspace import WorkspacePaths


@dataclass(frozen=True)
class CompanyData:
    name: str = ""
    name_en: str = ""
    industry: str = ""
    founded_year: int | None = None
    employee_current: int | None = None
    employee_joined_1y: int | None = None
    employee_left_1y: int | None = None
    employee_mom_change: float | None = None
    avg_salary: int | None = None
    salary_percentile: str | None = None
    revenue: float | None = None
    investment_round: str | None = None
    investment_total: float | None = None
    is_startup: bool = False
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    severity: str
    message: str


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str
    message: str
    value: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    file_path: Path
    company_name: str
    data: CompanyData
    issues: tuple[ValidationIssue, ...] = ()
    risk_flags: tuple[RiskFlag, ...] = ()
    completeness_score: float = 0.0


@dataclass(frozen=True)
class CompanyInfoLookup:
    status: str
    file_path: Path | None
    validation: ValidationResult | None
    digest: str | None


@dataclass(frozen=True)
class CompanyValidationSummary:
    processed_files: int
    error_files: int
    critical_risk_companies: int
    high_risk_companies: int
    incomplete_companies: int
    results: tuple[ValidationResult, ...]
    errors: tuple[str, ...]
    fixed_files: tuple[str, ...]
    report_path: Path | None = None


REQUIRED_FIELDS = (
    ("employee_current", "현재 인원"),
    ("founded_year", "설립연도"),
)

STARTUP_REQUIRED_FIELDS = (
    ("investment_round", "투자 라운드"),
    ("investment_total", "누적 투자금"),
    ("employee_joined_1y", "1년간 입사자"),
    ("employee_left_1y", "1년간 퇴사자"),
)

STARTUP_POSITIVE_KEYWORDS = (
    "스타트업",
    "Series",
    "시리즈",
    "Seed",
    "Pre-A",
    "벤처",
    "투자 유치",
    "누적 투자",
    "투자 라운드",
    "인원 급성장",
    "설립3년이하",
)

STARTUP_NEGATIVE_KEYWORDS = (
    "IPO",
    "M&A",
    "상장기업",
    "코스피",
    "코스닥",
    "KOSPI",
    "KOSDAQ",
    "대기업",
    "글로벌 기업",
    "한국법인",
    "계열",
    "일반기업",
    "해당 없음",
    "해당없음",
)

RISK_THRESHOLDS = {
    "turnover_critical": 0.5,
    "turnover_high": 0.3,
    "turnover_medium": 0.2,
    "shrinking_high": -0.1,
    "salary_low_percentile": 50,
    "company_young": 3,
}


def parse_company_file(file_path: Path) -> CompanyData:
    content = file_path.read_text(encoding="utf-8")
    data = CompanyData()
    startup_status_locked = False

    title_match = re.search(r"^#\s+(.+?)(?:\s*\((.+?)\))?\s*$", content, re.MULTILINE)
    if title_match:
        data = _replace(data, name=title_match.group(1).strip(), name_en=(title_match.group(2) or "").strip())

    info_section = re.search(r"## 기업 정보.*?(?=##|\Z)", content, re.DOTALL)
    if info_section:
        section_text = info_section.group()
        startup_match = re.search(r"스타트업\s*여부.*?\|\s*([^|\n]+)", section_text)
        if startup_match:
            startup_value = startup_match.group(1).strip().lower()
            if startup_value in {"yes", "y", "true", "1", "예", "맞음", "스타트업"}:
                data = _replace(data, is_startup=True)
                startup_status_locked = True
            elif startup_value in {"no", "n", "false", "0", "아니오", "아님", "비스타트업"}:
                data = _replace(data, is_startup=False)
                startup_status_locked = True
        year_match = re.search(r"설립.*?\|\s*(\d{4})년", section_text)
        if year_match:
            data = _replace(data, founded_year=int(year_match.group(1)))
        emp_match = re.search(r"직원수.*?\|\s*([\d,]+)명", section_text)
        if emp_match:
            data = _replace(data, employee_current=int(emp_match.group(1).replace(",", "")))
        industry_match = re.search(r"업종.*?\|\s*([^|]+)", section_text)
        if industry_match:
            data = _replace(data, industry=industry_match.group(1).strip())

    staff_section = re.search(r"## 인원 (?:통계|현황).*?(?=\n## [^#]|\Z)", content, re.DOTALL)
    if staff_section:
        section_text = staff_section.group()
        current_match = re.search(r"(?:현재 인원|총 인원).*?\|\s*약?\s*([\d,]+)명", section_text)
        if current_match:
            data = _replace(data, employee_current=int(current_match.group(1).replace(",", "")))
        joined_match = re.search(r"(?:1년간 입사자.*?\|\s*|입사[:\s]+)약?\s*([\d,]+)명", section_text)
        if joined_match:
            data = _replace(data, employee_joined_1y=int(joined_match.group(1).replace(",", "")))
        left_match = re.search(r"(?:1년간 퇴사자.*?\|\s*|퇴사[:\s]+)약?\s*([\d,]+)명", section_text)
        if left_match:
            data = _replace(data, employee_left_1y=int(left_match.group(1).replace(",", "")))
        mom_match = re.search(r"MoM\s*([-+]?\d+(?:\.\d+)?)\s*%", section_text)
        if mom_match:
            data = _replace(data, employee_mom_change=float(mom_match.group(1)))

    salary_section = re.search(r"## 연봉 정보.*?(?=##|\Z)", content, re.DOTALL)
    if salary_section:
        section_text = salary_section.group()
        salary_match = re.search(r"평균 연봉.*?\*\*(\d[\d,]*)만원\*\*", section_text)
        if salary_match:
            data = _replace(data, avg_salary=int(salary_match.group(1).replace(",", "")))
        percentile_match = re.search(r"상위\s*(\d+)%", section_text)
        if percentile_match:
            data = _replace(data, salary_percentile=percentile_match.group(1))

    investment_section = re.search(r"## 투자 정보.*?(?=##|\Z)", content, re.DOTALL)
    if investment_section:
        section_text = investment_section.group()
        if not startup_status_locked:
            data = _replace(data, is_startup=True)
        round_match = re.search(r"(?:현재 라운드|현재 상태).*?\|\s*([^\n|]+)", section_text)
        if round_match:
            round_val = round_match.group(1).strip()
            round_upper = round_val.upper()
            if "상장" in round_val:
                data = _replace(data, investment_round="IPO", is_startup=False)
                startup_status_locked = True
            elif "M&A" in round_upper or "MNA" in round_upper:
                data = _replace(data, investment_round="M&A", is_startup=False)
                startup_status_locked = True
            else:
                data = _replace(data, investment_round=round_val)
        total_match = re.search(r"누적 투자.*?\|\s*(?:약\s*)?([\d,]+(?:\.\d+)?)\s*억", section_text)
        if total_match:
            data = _replace(data, investment_total=float(total_match.group(1).replace(",", "")))

    if not startup_status_locked and ("TheVC" in content or "thevc.kr" in content):
        data = _replace(data, is_startup=True)
    if not startup_status_locked and any(keyword in content for keyword in STARTUP_POSITIVE_KEYWORDS):
        data = _replace(data, is_startup=True)
    neg_scope = info_section.group() if info_section else ""
    if not startup_status_locked and any(keyword in neg_scope for keyword in STARTUP_NEGATIVE_KEYWORDS):
        data = _replace(data, is_startup=False)

    revenue_section = re.search(r"## 매출.*?(?=##|\Z)", content, re.DOTALL)
    if revenue_section:
        section_text = revenue_section.group()
        revenue_match = re.search(r"매출액.*?([\d,]+(?:\.\d+)?)\s*억", section_text)
        if revenue_match:
            data = _replace(data, revenue=float(revenue_match.group(1).replace(",", "")))

    sources = tuple(sorted(set(re.findall(r"https?://[^\s\)]+", content))))
    return _replace(data, sources=sources)


def validate_company(data: CompanyData, file_path: Path, now: Optional[datetime] = None) -> ValidationResult:
    current_time = now or datetime.now()
    issues: list[ValidationIssue] = []
    risk_flags: list[RiskFlag] = []
    fields_present = 0
    total_fields = len(REQUIRED_FIELDS)

    for field_name, display_name in REQUIRED_FIELDS:
        value = getattr(data, field_name, None)
        if value is None:
            issues.append(ValidationIssue(field=field_name, severity="warning", message=f"{display_name} 누락"))
        else:
            fields_present += 1

    if data.is_startup:
        total_fields += len(STARTUP_REQUIRED_FIELDS)
        for field_name, display_name in STARTUP_REQUIRED_FIELDS:
            value = getattr(data, field_name, None)
            if value is None:
                issues.append(ValidationIssue(field=field_name, severity="warning", message=f"[스타트업] {display_name} 누락"))
            else:
                fields_present += 1

    completeness = (fields_present / total_fields * 100) if total_fields else 0.0

    if data.employee_current and data.employee_left_1y:
        turnover_rate = data.employee_left_1y / data.employee_current
        net_growth_ok = False
        net_info = ""
        if data.employee_joined_1y is not None:
            net_change = data.employee_joined_1y - data.employee_left_1y
            net_change_rate = net_change / data.employee_current if data.employee_current else 0
            if net_change_rate > RISK_THRESHOLDS["shrinking_high"]:
                net_growth_ok = True
            if net_change > 0:
                net_info = f" (순증 +{net_change}명, {net_change_rate:+.0%})"
            elif net_change < 0:
                net_info = f" (순감 {net_change}명, {net_change_rate:.0%})"

        if turnover_rate >= RISK_THRESHOLDS["turnover_critical"]:
            risk_flags.append(
                RiskFlag(
                    code="TURNOVER_HIGH" if net_growth_ok else "TURNOVER_CRITICAL",
                    severity="high" if net_growth_ok else "critical",
                    message=f"퇴사율 {turnover_rate:.0%} - 1년간 {data.employee_left_1y}명 퇴사 (현재 {data.employee_current}명){net_info}",
                    value=f"{turnover_rate:.0%}",
                )
            )
        elif turnover_rate >= RISK_THRESHOLDS["turnover_high"]:
            risk_flags.append(
                RiskFlag(
                    code="TURNOVER_MEDIUM" if net_growth_ok else "TURNOVER_HIGH",
                    severity="medium" if net_growth_ok else "high",
                    message=(
                        f"퇴사율 {turnover_rate:.0%}{net_info}"
                        if net_growth_ok
                        else f"퇴사율 {turnover_rate:.0%} - 조직 불안정 우려{net_info}"
                    ),
                    value=f"{turnover_rate:.0%}",
                )
            )
        elif turnover_rate >= RISK_THRESHOLDS["turnover_medium"] and not net_growth_ok:
            risk_flags.append(
                RiskFlag(
                    code="TURNOVER_MEDIUM",
                    severity="medium",
                    message=f"퇴사율 {turnover_rate:.0%}{net_info}",
                    value=f"{turnover_rate:.0%}",
                )
            )

    if data.employee_joined_1y is not None and data.employee_left_1y is not None and data.employee_current:
        net_change = data.employee_joined_1y - data.employee_left_1y
        net_change_pct = net_change / data.employee_current
        if net_change_pct < -0.2:
            risk_flags.append(RiskFlag(code="SHRINKING_FAST", severity="high", message=f"순감소 {net_change}명 ({net_change_pct:.0%}) - 조직 축소 중", value=f"{net_change}"))
        elif net_change_pct < -0.1:
            risk_flags.append(RiskFlag(code="SHRINKING", severity="medium", message=f"순감소 {net_change}명 - 인원 감소 추세", value=f"{net_change}"))

    if data.employee_mom_change is not None and data.employee_mom_change <= RISK_THRESHOLDS["shrinking_high"] * 100:
        risk_flags.append(RiskFlag(code="MOM_DECLINE", severity="medium", message=f"월간 인원 {data.employee_mom_change:+.1f}% 변동", value=f"{data.employee_mom_change:+.1f}%"))

    if data.salary_percentile:
        try:
            percentile = int(data.salary_percentile)
        except ValueError:
            percentile = None
        if percentile is not None and percentile > RISK_THRESHOLDS["salary_low_percentile"]:
            risk_flags.append(RiskFlag(code="SALARY_LOW", severity="medium", message=f"평균연봉 상위 {percentile}% - 업계 평균 이하", value=f"상위 {percentile}%"))

    if data.is_startup and data.founded_year:
        company_age = current_time.year - data.founded_year
        if company_age < RISK_THRESHOLDS["company_young"]:
            risk_flags.append(RiskFlag(code="EARLY_STAGE", severity="low", message=f"설립 {company_age}년차 - 초기 스타트업", value=f"{company_age}년"))
    if data.is_startup and data.investment_round is None and data.investment_total is None:
        risk_flags.append(RiskFlag(code="NO_INVESTMENT_DATA", severity="medium", message="투자 정보 없음 - 검증 필요"))

    return ValidationResult(
        file_path=file_path,
        company_name=data.name,
        data=data,
        issues=tuple(issues),
        risk_flags=tuple(risk_flags),
        completeness_score=completeness,
    )


def add_risk_section_to_markdown(content: str, result: ValidationResult, now: Optional[datetime] = None) -> str:
    if not result.risk_flags:
        return content
    current_time = now or datetime.now()
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    severity_icons = {"critical": "🚨", "high": "⚠️", "medium": "⚡", "low": "ℹ️"}
    lines = [
        "\n## ⚠️ 리스크 플래그\n",
        "| 수준 | 코드 | 내용 |",
        "|------|------|------|",
    ]
    for flag in sorted(result.risk_flags, key=lambda item: severity_order.get(item.severity, 99)):
        icon = severity_icons.get(flag.severity, "")
        lines.append(f"| {icon} {flag.severity.upper()} | {flag.code} | {flag.message} |")
    lines.append(f"\n*자동 생성: {current_time.strftime('%Y-%m-%d')}*\n")
    risk_section = "\n".join(lines)
    content = re.sub(r"\n## ⚠️ 리스크 플래그.*?(?=\n## |\n---|\Z)", "", content, flags=re.DOTALL)
    if "\n---\n\n*추출일:" in content:
        return content.replace("\n---\n\n*추출일:", f"{risk_section}\n---\n\n*추출일:")
    return content.rstrip() + risk_section


class CompanyInfoService:
    def __init__(self, *, workspace: WorkspacePaths) -> None:
        self.workspace = workspace
        self.company_info_dir = workspace.private_dir / "company_info"

    def validate(self, *, file_name: str | None = None, fix: bool = False, now: Optional[datetime] = None) -> CompanyValidationSummary:
        files = self._resolve_files(file_name)
        results: list[ValidationResult] = []
        errors: list[str] = []
        fixed_files: list[str] = []
        current_time = now or datetime.now()
        for file_path in files:
            try:
                result = validate_company(parse_company_file(file_path), file_path, now=current_time)
                results.append(result)
                if fix and result.risk_flags:
                    updated = add_risk_section_to_markdown(file_path.read_text(encoding="utf-8"), result, now=current_time)
                    if updated != file_path.read_text(encoding="utf-8"):
                        file_path.write_text(updated, encoding="utf-8")
                        fixed_files.append(file_path.name)
            except Exception as exc:
                errors.append(f"{file_path.name}: {exc}")
        return CompanyValidationSummary(
            processed_files=len(results),
            error_files=len(errors),
            critical_risk_companies=sum(1 for result in results if any(flag.severity == "critical" for flag in result.risk_flags)),
            high_risk_companies=sum(1 for result in results if any(flag.severity == "high" for flag in result.risk_flags)),
            incomplete_companies=sum(1 for result in results if result.completeness_score < 70),
            results=tuple(results),
            errors=tuple(errors),
            fixed_files=tuple(fixed_files),
        )

    def find_matching_file(self, company_name: str) -> Path | None:
        if not self.company_info_dir.is_dir():
            return None
        normalized = normalize_company_name(company_name)
        slug = slugify_company(company_name, fallback="")
        root = self.company_info_dir.resolve()
        for candidate in sorted(self.company_info_dir.glob("*.md")):
            if candidate.name.startswith("_"):
                continue
            try:
                path = candidate.resolve(strict=True)
                path.relative_to(root)
            except (OSError, ValueError):
                continue
            if path.suffix != ".md" or not path.is_file():
                continue
            if slug and slug in {
                slugify_company(candidate.stem, fallback=""),
                slugify_company(path.stem, fallback=""),
            }:
                return path
            try:
                parsed_name = parse_company_file(path).name
            except OSError:
                continue
            if normalized and normalize_company_name(parsed_name) == normalized:
                return path
        return None

    def inspect(self, company_name: str) -> CompanyInfoLookup:
        match = self._find_lookup_candidate(company_name)
        if match is None:
            return CompanyInfoLookup(status="missing", file_path=None, validation=None, digest=None)
        path, status = match
        if status == "unsafe":
            return CompanyInfoLookup(status="unsafe", file_path=None, validation=None, digest=None)
        try:
            content = path.read_text(encoding="utf-8")
            parsed = parse_company_file(path)
        except OSError:
            return CompanyInfoLookup(status="invalid", file_path=path, validation=None, digest=None)
        if not parsed.name.strip():
            return CompanyInfoLookup(status="invalid", file_path=path, validation=None, digest=_digest_text(content))
        validation = validate_company(parsed, path)
        lookup_status = "ready" if validation.completeness_score >= 70 else "incomplete"
        return CompanyInfoLookup(
            status=lookup_status,
            file_path=path,
            validation=validation,
            digest=_digest_text(content),
        )

    def apply_candidate(
        self,
        *,
        company_name: str,
        markdown: str,
        expected_digest: str | None = None,
        timeout: float = 1.0,
        before_validate: Callable[[Path], object] | None = None,
    ) -> CompanyInfoLookup:
        initial = self.inspect(company_name)
        if initial.status == "unsafe":
            raise ValueError("company info file must stay inside private/company_info")
        self.company_info_dir.mkdir(parents=True, exist_ok=True)
        target = initial.file_path or (self.company_info_dir / f"{slugify_company(company_name)}.md")
        with _CompanyFileLock(self._lock_path_for(target), timeout=timeout):
            _cleanup_hidden_stage_files(self.company_info_dir, target.stem)
            current_text = None
            if target.exists():
                safe_target = self._resolve_single_file(target)
                current_text = safe_target.read_text(encoding="utf-8")
            current_digest = _digest_text(current_text) if current_text is not None else None
            if expected_digest is not None and expected_digest != current_digest:
                raise RuntimeError("digest conflict")
            final_markdown = _merge_company_markdown(current_text, markdown)
            stage_path = self._stage_candidate(target.stem, final_markdown)
            try:
                if before_validate is not None:
                    before_validate(stage_path)
                validation = validate_company(parse_company_file(stage_path), stage_path)
                if not validation.company_name.strip():
                    raise ValueError("company info markdown must include a title")
                os.replace(stage_path, target)
                _fsync_file(target)
                _fsync_directory(self.company_info_dir)
            finally:
                if stage_path.exists():
                    stage_path.unlink()
                _cleanup_hidden_stage_files(self.company_info_dir, target.stem)
        return self.inspect(company_name)

    def _resolve_files(self, file_name: str | None) -> list[Path]:
        if file_name is not None:
            target = Path(file_name)
            candidate = target if target.is_absolute() else self.company_info_dir / target
            if candidate.is_symlink():
                raise FileNotFoundError(f"company info file not found: {file_name}")
            path = candidate.resolve()
            root = self.company_info_dir.resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("company info file must stay inside private/company_info") from exc
            if path.suffix != ".md" or not path.is_file():
                raise FileNotFoundError(f"company info file not found: {file_name}")
            return [path]
        if not self.company_info_dir.exists():
            raise FileNotFoundError(f"company info directory not found: {self.company_info_dir}")
        return sorted(
            path
            for path in self.company_info_dir.glob("*.md")
            if path.is_file() and not path.is_symlink() and not path.name.startswith("_")
        )

    def _find_lookup_candidate(self, company_name: str) -> tuple[Path, str] | None:
        if not self.company_info_dir.is_dir():
            return None
        normalized = normalize_company_name(company_name)
        slug = slugify_company(company_name, fallback="")
        root = self.company_info_dir.resolve()
        for candidate in sorted(self.company_info_dir.glob("*.md")):
            if candidate.name.startswith("_"):
                continue
            slug_matches = bool(slug) and slug in {
                slugify_company(candidate.stem, fallback=""),
                slugify_company(candidate.resolve(strict=False).stem, fallback=""),
            }
            if candidate.is_symlink():
                if not slug_matches:
                    continue
                try:
                    path = candidate.resolve(strict=True)
                    path.relative_to(root)
                except (OSError, ValueError):
                    return (candidate, "unsafe")
            try:
                path = candidate.resolve(strict=True)
                path.relative_to(root)
            except (OSError, ValueError):
                if slug_matches:
                    return (candidate, "unsafe")
                continue
            if path.suffix != ".md" or not path.is_file():
                continue
            if slug_matches:
                return (path, "file")
            try:
                parsed_name = parse_company_file(path).name
            except OSError:
                continue
            if normalized and normalize_company_name(parsed_name) == normalized:
                return (path, "file")
        return None

    def _lock_path_for(self, target: Path) -> Path:
        return self.company_info_dir / f".{target.stem}.lock"

    def _resolve_single_file(self, candidate: Path) -> Path:
        if candidate.is_symlink():
            raise ValueError("company info file must stay inside private/company_info")
        path = candidate.resolve(strict=True)
        root = self.company_info_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("company info file must stay inside private/company_info") from exc
        if path.suffix != ".md" or not path.is_file():
            raise FileNotFoundError(f"company info file not found: {candidate.name}")
        return path

    def _stage_candidate(self, stem: str, content: str) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix=f".{stem}.", suffix=".tmp", dir=self.company_info_dir)
        stage_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        return stage_path


class _CompanyFileLock:
    def __init__(self, path: Path, *, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._file = None

    def __enter__(self) -> "_CompanyFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + max(self.timeout, 0.0)
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    self._file.close()
                    raise TimeoutError("company info writer lock timeout") from exc
                time.sleep(0.01)

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        assert self._file is not None
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()


def _digest_text(content: str | None) -> str | None:
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _extract_section(content: str, heading: str) -> str | None:
    pattern = rf"(^## {re.escape(heading)}\n.*?)(?=^## |\n---\n|\Z)"
    match = re.search(pattern, content, flags=re.MULTILINE | re.DOTALL)
    return match.group(1).rstrip() if match else None


def _upsert_section(content: str, heading: str, replacement: str) -> str:
    pattern = rf"(^## {re.escape(heading)}\n.*?)(?=^## |\n---\n|\Z)"
    if re.search(pattern, content, flags=re.MULTILINE | re.DOTALL):
        return re.sub(pattern, replacement.rstrip() + "\n\n", content, count=1, flags=re.MULTILINE | re.DOTALL)
    footer_match = re.search(r"\n---\n", content)
    insertion = replacement.rstrip() + "\n\n"
    if footer_match:
        return content[: footer_match.start()] + "\n" + insertion + content[footer_match.start() :]
    return content.rstrip() + "\n\n" + insertion


def _merge_company_markdown(current_text: str | None, candidate_text: str) -> str:
    if current_text is None:
        return candidate_text.rstrip() + "\n"
    merged = current_text
    for heading in ("기업 정보", "인원 통계", "연봉 정보", "매출 정보", "투자 정보"):
        candidate_section = _extract_section(candidate_text, heading)
        if candidate_section:
            merged = _upsert_section(merged, heading, candidate_section)
    candidate_footer = _extract_footer(candidate_text)
    if candidate_footer:
        merged = _replace_footer(merged, candidate_footer)
    return merged.rstrip() + "\n"


def _extract_footer(content: str) -> str | None:
    if "\n---\n" not in content:
        return None
    return content[content.index("\n---\n") :].rstrip()


def _replace_footer(content: str, footer: str) -> str:
    if "\n---\n" in content:
        return content[: content.index("\n---\n")] + footer.rstrip() + "\n"
    return content.rstrip() + footer.rstrip() + "\n"


def _cleanup_hidden_stage_files(directory: Path, stem: str) -> None:
    for candidate in directory.glob(f".{stem}.*.tmp"):
        if candidate.is_file():
            candidate.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace(data: CompanyData, **changes: object) -> CompanyData:
    return replace(data, **changes)
