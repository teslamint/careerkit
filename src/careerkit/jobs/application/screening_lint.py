from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import TextIO

from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordIntegrityError,
    JobRecordNotFound,
)
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, PostingStatus, ScreeningVerdict
from careerkit.jobs.domain.verdict import parse_verdict_from_screening


HEADHUNTER_KEYWORDS = ("써치펌", "서치펌", "헤드헌팅", "헤드헌터", "고용 알선", "알선업", "채용대행")
CUT_CONTEXT = re.compile(r"❌|즉시 컷|자동 배제|자동 제외|컷 사유|하드 컷|비추천 사유")
DISPATCH_PATTERN = re.compile(r"파견")
CLOSED_MARKERS = ("채용 마감", "채용이 마감", "마감되었습니다", "이 공고는 마감", "지원 기간이 종료", "상시채용 종료", "Position closed")
CRYPTO_PATTERN = re.compile(r"가상자산|암호화폐|블록체인 거래소|코인 거래소|\bDEX\b|\bDeFi\b")
STACK_CUT_PATTERN = re.compile(r"(스택|기술)\s?(불일치|미스매치|상이).{0,20}(❌|컷)|(❌|컷).{0,20}(스택|기술)\s?(불일치|미스매치)")
DOMAIN_CUT_PATTERN = re.compile(r"(도메인|직무)[^\n]{0,40}(❌|미스매치|불일치|부적합)|(❌|컷)[^\n]{0,30}도메인|비백엔드|백엔드(가|/서버가)?\s?아니|산업 리스크[^\n]{0,30}(❌|컷)|가상자산[^\n]{0,40}(즉시 컷|❌)")
PATCH_PATH_PATTERN = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)
_VERDICT_LABEL = {
    ScreeningVerdict.RECOMMENDED: "지원 추천",
    ScreeningVerdict.HOLD: "지원 보류",
    ScreeningVerdict.NOT_RECOMMENDED: "지원 비추천",
}


@dataclass(frozen=True)
class Finding:
    level: str
    check: str
    file: str
    detail: str

    def render(self) -> str:
        return f"{'✗' if self.level == 'violation' else '⚠'} [{self.check}] {self.file}: {self.detail}"


@dataclass(frozen=True)
class LintReport:
    findings: tuple[Finding, ...]
    keys_checked: int

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.level == "warn")

    @property
    def violations(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.level == "violation")

    @property
    def exit_code(self) -> int:
        return 2 if self.violations else 0

    def summary(self) -> str:
        return f"검사 {self.keys_checked}건: 위반 {len(self.violations)}, 경고 {len(self.warnings)}"


def lint_record(key: JobKey, repository: JDRecordRepository) -> list[Finding]:
    name = f"{key.platform}:{key.job_id}"
    try:
        stored = repository.get(key)
    except JobRecordNotFound:
        return [Finding("warn", "jd-missing", name, "canonical JD record 없음 (orphan)")]
    except (JobRecordIntegrityError, OSError, UnicodeError, ValueError) as exc:
        return [Finding("violation", "storage-corrupt", name, _safe_detail(str(exc), repository.root))]
    if stored.screening_markdown is None:
        return []

    record = stored.record
    text = stored.screening_markdown
    jd_text = stored.jd_markdown
    findings: list[Finding] = []
    verdict = parse_verdict_from_screening(text)
    if verdict is None:
        findings.append(Finding("violation", "verdict-missing", name, "최종 판정을 파싱할 수 없음"))

    protected = record.application_status is not ApplicationStatus.PENDING
    if (
        verdict is not None
        and record.screening_verdict is not None
        and record.posting_status is not PostingStatus.CLOSED
        and not protected
        and verdict != _VERDICT_LABEL[record.screening_verdict]
    ):
        findings.append(
            Finding(
                "violation",
                "verdict-metadata-mismatch",
                name,
                f"본문 판정 '{verdict}' != 메타데이터 '{_VERDICT_LABEL[record.screening_verdict]}'",
            )
        )
    if any(marker in jd_text for marker in CLOSED_MARKERS) and record.posting_status is not PostingStatus.CLOSED:
        findings.append(Finding("violation", "closed-keyword-not-classified", name, "JD 마감 키워드가 있으나 posting_status가 active"))
    if verdict == "지원 비추천" and record.posting_status is not PostingStatus.CLOSED and record.application_status is not ApplicationStatus.REJECTED:
        for line in text.splitlines():
            if re.search(r"무효|정정", line):
                continue
            if any(keyword in line for keyword in HEADHUNTER_KEYWORDS) and CUT_CONTEXT.search(line):
                if not DISPATCH_PATTERN.search(jd_text):
                    if DOMAIN_CUT_PATTERN.search(text):
                        findings.append(Finding("warn", "headhunter-cut-review", name, "써치펌 컷과 도메인 컷 병존 — 수동 확인"))
                    else:
                        findings.append(Finding("violation", "headhunter-hard-cut", name, "써치펌 컷이나 JD에 파견 명시 없음"))
                break
        if STACK_CUT_PATTERN.search(text):
            findings.append(Finding("warn", "stack-cut-review", name, "스택 불일치 단독 컷 여부 수동 확인"))
    if verdict == "지원 추천" and CRYPTO_PATTERN.search(jd_text):
        findings.append(Finding("warn", "crypto-industry-recommended", name, "가상자산 산업 즉시 컷 해당 여부 수동 확인"))
    return findings


def key_from_screening_path(path: Path, records_root: Path) -> JobKey | None:
    try:
        relative = path.resolve().relative_to(records_root.resolve())
    except ValueError:
        return None
    if path.name != "screening.md" or len(relative.parts) < 4 or relative.parts[2] != "content":
        return None
    try:
        return JobKey(relative.parts[0], relative.parts[1])
    except ValueError:
        return None


def hook_target_paths(*, payload: dict[str, object], records_root: Path) -> list[Path]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    cwd_raw = payload.get("cwd")
    cwd = Path(cwd_raw) if isinstance(cwd_raw, str) and cwd_raw else Path.cwd()
    raw_paths: list[str] = []
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str):
        raw_paths.append(file_path)
    if payload.get("tool_name") == "apply_patch":
        command = tool_input.get("command")
        if isinstance(command, str):
            for match in PATCH_PATH_PATTERN.finditer(command):
                raw_paths.append(match.group(1) or match.group(2))
    targets: list[Path] = []
    for raw_path in raw_paths:
        target = _screening_path(raw_path, cwd, records_root=records_root)
        if target is not None and target not in targets:
            targets.append(target)
    return targets


def hook_keys_from_stdin(stdin: TextIO, *, records_root: Path) -> list[JobKey]:
    try:
        payload = json.load(stdin)
    except (json.JSONDecodeError, OSError):
        return []
    return [
        key
        for path in hook_target_paths(payload=payload, records_root=records_root)
        if (key := key_from_screening_path(path, records_root)) is not None
    ]


def run(keys: list[JobKey], repository: JDRecordRepository) -> LintReport:
    findings = tuple(finding for key in keys for finding in lint_record(key, repository))
    return LintReport(findings=findings, keys_checked=len(keys))


def render_report(report: LintReport, *, stdout: TextIO, stderr: TextIO) -> int:
    for finding in report.warnings:
        print(finding.render(), file=stdout)
    for finding in report.violations:
        print(finding.render(), file=stderr)
    print(report.summary(), file=stderr if report.violations else stdout)
    return report.exit_code


def _screening_path(raw_path: str, cwd: Path, *, records_root: Path) -> Path | None:
    path = Path(raw_path)
    path = (cwd / path if not path.is_absolute() else path).resolve()
    return path if path.exists() and key_from_screening_path(path, records_root) is not None else None


def _safe_detail(detail: str, records_root: Path) -> str:
    safe = detail.replace(str(records_root.resolve()), "private/jd/records")
    return safe.replace("\\", "/")
