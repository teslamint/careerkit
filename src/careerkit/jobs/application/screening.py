from __future__ import annotations

from dataclasses import dataclass, field
import importlib.resources as resources
import json
from pathlib import Path
import re
from typing import Optional

from careerkit.jobs.adapters.screening.cli_provider import CLIProvider, LLMProvider
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, StoredJobRecord
from careerkit.jobs.application.company_info import parse_company_file, validate_company
from careerkit.jobs.application.evidence_checks import (
    apply_demotions,
    check_rows,
    corpus_basenames,
    corpus_source_paths,
    parse_match_table,
)
from careerkit.jobs.application.requirement_manifest import (
    RequirementManifest,
    aggregate_parent_matches,
    extract_requirement_manifest,
    without_main_duty,
)
from careerkit.jobs.application.screening_assessment import (
    AssessmentContractError,
    parse_screening_assessment,
    render_screening_markdown,
)
from careerkit.jobs.application.storage_migration import extract_metadata_from_jd
from careerkit.jobs.domain.verdict import (
    VERDICT_PRIORITY,
    parse_verdict_candidates,
    parse_verdict_from_screening,
    to_screening_verdict,
)
from careerkit.workspace import WorkspacePaths

MAX_FALLBACK_REASON_CHARS = 240
_SENSITIVE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]*(?:SECRET|TOKEN|KEY)[A-Za-z0-9_-]*")
_REQUIRED_SECTIONS = (
    "## 기본 정보",
    "## 스크리닝 결과",
    "## 이력/경험 매칭",
    "## 최종 판정",
    "## 핵심 근거",
)
_CONVERSATIONAL_PATTERNS = (
    "승인 대기 중",
    "권한을 요청합니다",
    "저장 권한이 필요",
    "실행 권한이 필요",
    "Plan 파일을 작성",
    "진행 방식을 확인하겠습니다",
    "어디에 저장할까",
    "저장할 위치를 알려",
    "진행해도 될까",
    "스크리닝을 진행하겠습니다",
    "분석을 진행하겠습니다",
    "도와드리겠습니다",
    "해드리겠습니다",
)
_MIN_CONTENT_LINES = 5
_FALLBACK_MARKER = "| 생성 방식 | 자동 fallback |"
_FALLBACK_MATCH_HEADER = "| 항목 | 판단 |"
_FALLBACK_MATCH_ROWS = ("| 후보자 이력 대조 |", "| JD 필수요건 대조 |")
_FALLBACK_ATTEMPT_MARKERS = (
    "| 생성 방식 | 자동 fallback |",
    "| 항목 | 판단 |",
    "| 후보자 이력 대조 |",
    "| JD 필수요건 대조 |",
    "LLM 스크리닝 실행이 완료되지 않아 자동 판정은 보류로 기록한다.",
    "- 자동 분석 경로에서 LLM 응답을 얻지 못했다.",
    "| 후보자 이력 대조 |",
    "| JD 필수요건 대조 |",
)
_FILLER_PREFIXES = ("|---", "|-", "| ---", "| -")
_PROMPT_PACKAGE = "careerkit.jobs.resources.prompts"
_SCREENING_RULES_PATH = Path("private/jd/config/jd-screening-rules.md")
_BASIC_INFO_SECTION = "## 기본 정보"
_CANDIDATE_INFO_LABEL_PREFIXES = ("후보자", "지원자")
_VERDICT_LINE_RE = re.compile(
    r"^("
    r"[ \t]*#{1,6}[ \t]*최종[ \t]*판정(?:[ \t]*[:：\-][ \t]*|[ \t]+)"
    r"|[ \t]*-[ \t]*\*\*최종[ \t]*판정\*\*[ \t]*[:：][ \t]*"
    r").+$",
    re.MULTILINE,
)
LOCAL_PROVIDER_LABELS = frozenset({"ollama", "local"})
STRONG_PROVIDER_LABELS = frozenset({"claude", "codex"})
REQUIRED_MISSING_THRESHOLD = 2
_HOLD_VERDICT = "지원 보류"
_NOT_RECOMMENDED = "지원 비추천"
_RECOMMENDED = "지원 추천"


@dataclass(frozen=True)
class ScreeningResult:
    verdict: str
    screening_path: Path
    provider: str
    used_fallback: bool
    raw_output: str
    fallback_reason: str | None = None
    verdict_capped: bool = False
    downgraded: bool = False
    evidence_violations: dict[str, int] = field(default_factory=dict)
    provider_attempts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    context_warning: str | None = None
    published: bool = False


def load_screening_rules(workspace: WorkspacePaths) -> str:
    path = workspace.root / _SCREENING_RULES_PATH
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _load_text(path: Optional[Path]) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_prompt_template() -> str:
    return resources.files(_PROMPT_PACKAGE).joinpath("screening_system.txt").read_text(encoding="utf-8")


def _serialize_manifest(manifest: RequirementManifest) -> str:
    payload = {
        "schema_version": 1,
        "decision_basis_parent_ids": [item.id for item in manifest.parents],
        "match_targets": [
            {
                "id": item.id,
                "text": item.text,
                "kind": item.kind.value,
                "parent_id": item.parent_id,
                "decisive": item.decisive,
            }
            for item in manifest.leaves
            if item.assessable
        ],
        "ambiguous_qualifications": manifest.ambiguous_qualifications,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_prompt(
    *,
    workspace: WorkspacePaths,
    jd_content: str,
    rules: str,
    company_content: str,
    company_risk_summary: str,
    candidate_context: str,
    manifest: RequirementManifest,
) -> str:
    template = load_prompt_template()
    return template.format(
        rules=rules,
        candidate_context=candidate_context,
        company_risk_summary=company_risk_summary,
        company_content=company_content,
        jd_content=jd_content,
        requirement_manifest=_serialize_manifest(manifest),
    )


def build_company_risk_summary(company_file: Optional[Path]) -> str:
    if company_file is None or not company_file.exists():
        return "기업 정보 파일 없음"
    text = company_file.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return "기업 정보 파일이 비어 있음"
    try:
        result = validate_company(parse_company_file(company_file), company_file)
    except (OSError, ValueError):
        return "기업 리스크 요약 생성 실패"
    if not result.risk_flags:
        return f"완성도 {result.completeness_score}점; 검출된 리스크 없음"
    lines = [f"완성도 {result.completeness_score}점"]
    lines.extend(
        f"- {flag.severity.upper()} {flag.code}: {flag.message}"
        for flag in result.risk_flags
    )
    return "\n".join(lines)


def _is_substantive_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    for prefix in _FILLER_PREFIXES:
        if stripped.startswith(prefix) and set(stripped.replace("|", "").strip()) <= {"-", " "}:
            return False
    return True


def validate_screening_structure(markdown: str) -> tuple[bool, str]:
    lines = markdown.splitlines()
    heading_lines = {line.strip() for line in lines if line.strip().startswith("#")}
    missing = [section for section in _REQUIRED_SECTIONS if section not in heading_lines]
    if missing:
        return False, f"필수 섹션 누락: {', '.join(missing)}"

    if is_fallback_document(markdown):
        return True, ""

    for pattern in _CONVERSATIONAL_PATTERNS:
        if pattern in markdown:
            return False, f"대화형 패턴 탐지: '{pattern}'"

    content_lines = [line for line in lines if _is_substantive_line(line)]
    if len(content_lines) < _MIN_CONTENT_LINES:
        return False, f"섹션 내용 부족 (헤더/구분선 제외 {len(content_lines)}줄 < {_MIN_CONTENT_LINES})"

    candidate_label = _basic_info_candidate_label(markdown)
    if candidate_label is not None:
        return False, f"기본 정보에 후보자 개인정보 행 포함: {candidate_label}"

    if _looks_like_fallback_attempt(markdown):
        return False, "fallback 문서 계약 위반"

    _, table_error = parse_match_table(markdown)
    if table_error:
        return False, table_error

    return True, ""


def is_fallback_document(markdown: str) -> bool:
    """Whether this is the document build_fallback_output writes, whole and not in part."""
    parsed = _parse_fallback_document(markdown)
    if parsed is None:
        return False
    return markdown == _render_fallback_document(
        file_id=parsed["file_id"],
        company=parsed["company"],
        position=parsed["position"],
        source_url=parsed["source_url"],
        reason=parsed["reason"],
    )


def _looks_like_fallback_attempt(markdown: str) -> bool:
    return any(marker in markdown for marker in _FALLBACK_ATTEMPT_MARKERS)


def _split_table_row(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line)
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _parse_two_column_row(line: str, *, label: str) -> str | None:
    if not line.startswith("|"):
        return None
    cells = _split_table_row(line)
    if len(cells) != 2 or cells[0] != label:
        return None
    value = cells[1].replace("\\|", "|")
    if value != "확인 필요" and not value:
        return None
    return value


def _render_fallback_document(
    *,
    file_id: str,
    company: str,
    position: str,
    source_url: str,
    reason: str,
) -> str:
    return f"""## 기본 정보

| 항목 | 내용 |
|------|------|
| 파일 | {_table_cell(file_id)} |
| 회사명 | {_table_cell(company)} |
| 포지션 | {_table_cell(position)} |
| 출처 | {_table_cell(source_url)} |
{_FALLBACK_MARKER}

## 스크리닝 결과

LLM 스크리닝 실행이 완료되지 않아 자동 판정은 보류로 기록한다. 채용 적합성은 수동 재스크리닝 전까지 확정하지 않는다.

## 이력/경험 매칭

| 항목 | 판단 |
|------|------|
| 후보자 이력 대조 | LLM 분석 실패로 이력 근거 대조가 수행되지 않았다. |
| JD 필수요건 대조 | 수동 재스크리닝 필요. |

## 최종 판정

### 최종 판정: 지원 보류

## 핵심 근거

- 자동 분석 경로에서 LLM 응답을 얻지 못했다.
- 실패 사유: {_table_cell(reason, '알 수 없음')}
- 이 문서는 원시 실행 로그를 저장하지 않고 수동 재스크리닝을 위한 보류 상태만 기록한다.
"""


def _parse_fallback_document(markdown: str) -> dict[str, str] | None:
    lines = markdown.splitlines()
    expected_prefix = [
        "## 기본 정보",
        "",
        "| 항목 | 내용 |",
        "|------|------|",
    ]
    if lines[:4] != expected_prefix:
        return None
    if len(lines) != 30:
        return None

    file_id = _parse_two_column_row(lines[4], label="파일")
    company = _parse_two_column_row(lines[5], label="회사명")
    position = _parse_two_column_row(lines[6], label="포지션")
    source_url = _parse_two_column_row(lines[7], label="출처")
    marker = _parse_two_column_row(lines[8], label="생성 방식")
    if None in {file_id, company, position, source_url, marker}:
        return None
    assert file_id is not None
    assert company is not None
    assert position is not None
    assert source_url is not None
    if marker != "자동 fallback":
        return None

    exact_lines = {
        9: "",
        10: "## 스크리닝 결과",
        11: "",
        12: "LLM 스크리닝 실행이 완료되지 않아 자동 판정은 보류로 기록한다. 채용 적합성은 수동 재스크리닝 전까지 확정하지 않는다.",
        13: "",
        14: "## 이력/경험 매칭",
        15: "",
        16: "| 항목 | 판단 |",
        17: "|------|------|",
        18: "| 후보자 이력 대조 | LLM 분석 실패로 이력 근거 대조가 수행되지 않았다. |",
        19: "| JD 필수요건 대조 | 수동 재스크리닝 필요. |",
        20: "",
        21: "## 최종 판정",
        22: "",
        23: "### 최종 판정: 지원 보류",
        24: "",
        25: "## 핵심 근거",
        26: "",
        27: "- 자동 분석 경로에서 LLM 응답을 얻지 못했다.",
    }
    for index, expected in exact_lines.items():
        if lines[index] != expected:
            return None

    if not lines[28].startswith("- 실패 사유: "):
        return None
    if lines[29] != "- 이 문서는 원시 실행 로그를 저장하지 않고 수동 재스크리닝을 위한 보류 상태만 기록한다.":
        return None
    reason = lines[28].removeprefix("- 실패 사유: ")
    if not reason or re.search(r"(?<!\\)\|", reason):
        return None
    reason = reason.replace("\\|", "|")

    return {
        "file_id": file_id,
        "company": company,
        "position": position,
        "source_url": source_url,
        "reason": reason,
    }


def _basic_info_candidate_label(markdown: str) -> Optional[str]:
    start = markdown.find(_BASIC_INFO_SECTION)
    if start < 0:
        return None
    body = markdown[start + len(_BASIC_INFO_SECTION) :]
    end = body.find("\n## ")
    if end >= 0:
        body = body[:end]
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        label = line.strip("|").split("|")[0].strip()
        if label.startswith(_CANDIDATE_INFO_LABEL_PREFIXES):
            return label
    return None


def rewrite_verdict_line(markdown: str, verdict: str) -> str:
    updated, count = _VERDICT_LINE_RE.subn(lambda m: f"{m.group(1)}{verdict}", markdown)
    if count == 0:
        raise ValueError("최종 판정 줄을 찾을 수 없음")
    return updated


def _inject_ambiguous_placeholder_row(markdown: str) -> str:
    header = "| 요건 | 구분 | 대조 | 근거 |\n|------|------|------|------|\n"
    placeholder = "| 서술형 자격요건 | 필수 | 부분 | 서술형 자격요건은 수동 확인 필요 |"
    if header not in markdown or placeholder in markdown:
        return markdown
    return markdown.replace(header, header + placeholder + "\n", 1)


def _table_cell(value: Optional[str], default: str = "확인 필요") -> str:
    text = (value or default).strip() or default
    return text.replace("|", "\\|").replace("\n", " ")


def summarize_llm_error(exc: Exception) -> str:
    message = _SENSITIVE_TOKEN_RE.sub("[redacted]", str(exc)).replace("\r", "\n").strip()
    if not message:
        return "LLM 실행 오류"
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if len(first_line) > MAX_FALLBACK_REASON_CHARS:
        first_line = first_line[:MAX_FALLBACK_REASON_CHARS].rstrip() + "..."
    return first_line or "LLM 실행 오류"


def build_fallback_output(jd: StoredJobRecord, jd_content: str, reason: str) -> str:
    metadata = extract_metadata_from_jd(jd_content)
    company = metadata.get("company")
    position = metadata.get("position") or jd.record.position
    source_url = metadata.get("url")

    return f"""## 기본 정보

| 항목 | 내용 |
|------|------|
| 파일 | {_table_cell(f'{jd.record.platform}/{jd.record.job_id}')} |
| 회사명 | {_table_cell(company)} |
| 포지션 | {_table_cell(position)} |
| 출처 | {_table_cell(source_url)} |
{_FALLBACK_MARKER}

## 스크리닝 결과

LLM 스크리닝 실행이 완료되지 않아 자동 판정은 보류로 기록한다. 채용 적합성은 수동 재스크리닝 전까지 확정하지 않는다.

## 이력/경험 매칭

| 항목 | 판단 |
|------|------|
| 후보자 이력 대조 | LLM 분석 실패로 이력 근거 대조가 수행되지 않았다. |
| JD 필수요건 대조 | 수동 재스크리닝 필요. |

## 최종 판정

### 최종 판정: 지원 보류

## 핵심 근거

- 자동 분석 경로에서 LLM 응답을 얻지 못했다.
- 실패 사유: {_table_cell(reason, '알 수 없음')}
- 이 문서는 원시 실행 로그를 저장하지 않고 수동 재스크리닝을 위한 보류 상태만 기록한다.
"""


def _assessment_retry_prefix(error: str) -> str:
    return (
        "이전 응답이 JSON 계약을 위반했습니다. "
        "JSON 객체 하나만 다시 출력하세요. "
        f"오류: {error}\n\n"
    )


def _decision_basis_supports_not_recommended(manifest: RequirementManifest, assessment) -> bool:
    parent_matches = aggregate_parent_matches(
        manifest,
        {item.id: item.match for item in assessment.matches},
    )
    parent_map = {item.id: item for item in manifest.parents}
    for basis_id in assessment.decision_basis:
        parent = parent_map[basis_id]
        if parent.kind.value != "필수" or not parent.decisive:
            continue
        if parent_matches[basis_id] == "없음":
            return True
    return False


def _apply_conservative_verdict_guard(
    *,
    manifest: RequirementManifest,
    assessment,
    rows,
    verdict: str,
    provider: str,
    evidence_violations: dict[str, int],
) -> tuple[str, bool, bool]:
    hold_floor = VERDICT_PRIORITY[_HOLD_VERDICT]
    verdict_capped = False
    downgraded = False

    provider_recommended = assessment.verdict == _RECOMMENDED
    provider_not_recommended = assessment.verdict == _NOT_RECOMMENDED
    required_missing = sum(1 for row in rows if row.kind == "필수" and row.match == "없음")

    if provider_recommended and required_missing >= REQUIRED_MISSING_THRESHOLD:
        if VERDICT_PRIORITY[verdict] > hold_floor:
            verdict = _HOLD_VERDICT
        downgraded = True

    if provider in LOCAL_PROVIDER_LABELS and provider_recommended:
        if VERDICT_PRIORITY[verdict] > hold_floor:
            verdict = _HOLD_VERDICT
        verdict_capped = True

    supported_not_recommended = _decision_basis_supports_not_recommended(manifest, assessment)
    if provider_not_recommended and not supported_not_recommended:
        verdict = _HOLD_VERDICT
        evidence_violations["unsupported_not_recommended"] = (
            evidence_violations.get("unsupported_not_recommended", 0) + 1
        )

    if manifest.ambiguous_qualifications and not supported_not_recommended:
        verdict = _HOLD_VERDICT
        downgraded = True

    return verdict, verdict_capped, downgraded
def run_screening(
    *,
    workspace: WorkspacePaths,
    jd: StoredJobRecord,
    company_file: Optional[Path],
    llm_timeout: int = 120,
    local_llm_timeout: int | None = None,
    dry_run: bool = False,
    llm_provider: Optional[LLMProvider] = None,
    repository: JDRecordRepository | None = None,
    candidate_context: str | None = None,
    require_strong_provider: bool = False,
) -> ScreeningResult:
    jd_content = jd.jd_markdown
    rules = load_screening_rules(workspace)
    company_content = _load_text(company_file)
    risk_summary = build_company_risk_summary(company_file)
    candidate_context_text = candidate_context or "후보자 이력/경험 근거는 호출자가 제공하지 않았음"
    manifest = extract_requirement_manifest(jd_content)
    filtered = without_main_duty(manifest)
    if not any(item.assessable for item in filtered.leaves):
        raise ValueError("screening-no-assessable-requirements")

    prompt = build_prompt(
        workspace=workspace,
        jd_content=jd_content,
        rules=rules,
        company_content=company_content,
        company_risk_summary=risk_summary,
        candidate_context=candidate_context_text,
        manifest=filtered,
    )
    provider_runner = llm_provider if llm_provider is not None else CLIProvider()
    reset_observations = getattr(provider_runner, "reset_observations", None)
    if callable(reset_observations):
        reset_observations()

    provider = "fallback"
    used_fallback = False
    raw_output = ""
    fallback_reason: str | None = None
    verdict = _HOLD_VERDICT
    normalized_output = ""
    verdict_capped = False
    downgraded = False
    evidence_violations: dict[str, int] = {}

    effective_local_llm_timeout = local_llm_timeout if local_llm_timeout is not None else llm_timeout

    try:
        provider, raw_output = provider_runner.run(
            prompt,
            timeout=llm_timeout,
            local_timeout=effective_local_llm_timeout,
        )
    except Exception as exc:
        used_fallback = True
        fallback_reason = "provider-exhausted"
        reason = summarize_llm_error(exc)
    else:
        try:
            assessment = parse_screening_assessment(raw_output, filtered)
        except AssessmentContractError as first_error:
            try:
                provider, raw_output = provider_runner.run(
                    _assessment_retry_prefix(str(first_error)) + prompt,
                    timeout=llm_timeout,
                    local_timeout=effective_local_llm_timeout,
                )
                assessment = parse_screening_assessment(raw_output, filtered)
            except AssessmentContractError as retry_error:
                used_fallback = True
                fallback_reason = "assessment-contract-exhausted"
                reason = summarize_llm_error(retry_error)
            except Exception as retry_error:
                used_fallback = True
                fallback_reason = "provider-exhausted"
                reason = summarize_llm_error(retry_error)

    if used_fallback:
        used_fallback = True
        provider = "fallback"
        verdict = _HOLD_VERDICT
        verdict_capped = False
        downgraded = False
        evidence_violations = {}
        raw_output = f"LLM 스크리닝 실패: {reason}"
        normalized_output = build_fallback_output(jd, jd_content, reason)
        valid, reason = validate_screening_structure(normalized_output)
        if not valid:
            raise RuntimeError(f"구조 검증 실패: {reason}")
    else:
        normalized_output = render_screening_markdown(jd, jd_content, filtered, assessment)
        if filtered.ambiguous_qualifications and not filtered.parents:
            normalized_output = _inject_ambiguous_placeholder_row(normalized_output)
        rows, table_error = parse_match_table(normalized_output)
        if table_error:
            raise RuntimeError(f"매칭 표 파싱 실패: {table_error}")
        if rows:
            basenames = corpus_basenames(candidate_context_text)
            declared = corpus_source_paths(candidate_context_text)
            report = check_rows(
                rows,
                corpus=candidate_context_text,
                root=workspace.root,
                declared=declared,
                basenames=basenames,
            )
            evidence_violations = {
                "missing_source_path": report.missing_source_path,
                "unevidenced_keyword": report.unevidenced_keyword,
                "unevidenced_keyword_strict": report.unevidenced_keyword_strict,
            }
            if report.demoted_indices:
                normalized_output = apply_demotions(normalized_output, rows, report.demoted_indices)
                rows, demotion_error = parse_match_table(normalized_output)
                if demotion_error:
                    raise RuntimeError(f"강등 적용 후 표 파싱 실패: {demotion_error}")
        else:
            evidence_violations = {
                "missing_source_path": 0,
                "unevidenced_keyword": 0,
                "unevidenced_keyword_strict": 0,
            }

        evidence_violations["unevidenced_main_duty"] = sum(
            1 for row in rows if row.kind == "주요업무" and row.match == "없음"
        )
        verdict, verdict_capped, downgraded = _apply_conservative_verdict_guard(
            manifest=filtered,
            assessment=assessment,
            rows=rows,
            verdict=assessment.verdict,
            provider=provider,
            evidence_violations=evidence_violations,
        )
        if verdict != assessment.verdict:
            normalized_output = rewrite_verdict_line(normalized_output, verdict)
        valid, reason = validate_screening_structure(normalized_output)
        if not valid:
            raise RuntimeError(f"구조 검증 실패: {reason}")
        reparsed = parse_verdict_from_screening(normalized_output)
        if reparsed != verdict:
            raise RuntimeError(f"판정 재작성 후 파싱 불일치: 기대 {verdict}, 파싱 {reparsed}")
        residual = [
            candidate
            for candidate in parse_verdict_candidates(normalized_output)
            if VERDICT_PRIORITY[candidate] > VERDICT_PRIORITY[verdict]
        ]
        if residual:
            raise RuntimeError(
                f"판정 재작성 후 상위 판정 잔존: 기대 {verdict}, 잔존 {sorted(set(residual))}"
            )

    screening_path = Path(jd.record.platform) / jd.record.job_id / "screening.md"
    withheld = require_strong_provider and (used_fallback or provider not in STRONG_PROVIDER_LABELS)
    published = False

    if not dry_run and not withheld:
        if repository is None:
            raise ValueError("repository is required when dry_run is false")
        canonical_verdict = to_screening_verdict(verdict)
        if canonical_verdict is None:
            raise ValueError(f"Unsupported screening verdict: {verdict}")
        stored_after = repository.update_screening_result(
            jd.record.key,
            screening_markdown=normalized_output.rstrip() + "\n",
            screening_verdict=canonical_verdict,
            screening_provider=provider,
            verdict_capped=None if used_fallback else verdict_capped,
        )
        if used_fallback:
            verdict_capped = bool(stored_after.record.verdict_capped)
        published_path = repository.screening_path(jd.record.key)
        if published_path is None:
            raise RuntimeError("screening content was not published")
        screening_path = published_path
        published = True

    return ScreeningResult(
        verdict=verdict,
        screening_path=screening_path,
        provider=provider,
        used_fallback=used_fallback,
        raw_output=raw_output,
        fallback_reason=fallback_reason,
        verdict_capped=verdict_capped,
        downgraded=downgraded,
        evidence_violations=evidence_violations,
        provider_attempts={
            label: tuple(details)
            for label, details in (getattr(provider_runner, "last_attempts", {}) or {}).items()
        },
        context_warning=getattr(provider_runner, "last_context_warning", None),
        published=published,
    )
