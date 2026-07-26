from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application import screening_lint
from careerkit.jobs.application.screening import build_fallback_output
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)


def _screening(verdict: str, extra: str = "") -> str:
    return f"# screening\n{extra}\n## 최종 판정\n### 최종 판정: {verdict}\n"


def _stored(
    root: Path,
    *,
    verdict: ScreeningVerdict = ScreeningVerdict.RECOMMENDED,
    application: ApplicationStatus = ApplicationStatus.PENDING,
    posting: PostingStatus = PostingStatus.ACTIVE,
    body_verdict: str = "지원 추천",
    jd: str = "# JD",
) -> tuple[JDRecordRepository, JobKey]:
    repository = JDRecordRepository(root)
    record = JobRecord(
        platform="wanted",
        job_id="12345",
        company="Acme",
        position="Backend",
        screening_verdict=verdict,
        application_status=application,
        posting_status=posting,
    )
    repository.create(record, jd_markdown=jd)
    repository.update_screening_result(record.key, screening_markdown=_screening(body_verdict))
    return repository, record.key


def test_metadata_and_screening_verdict_mismatch_is_violation(tmp_path: Path) -> None:
    repository, key = _stored(
        tmp_path / "records",
        verdict=ScreeningVerdict.RECOMMENDED,
        body_verdict="지원 비추천",
    )

    findings = screening_lint.lint_record(key, repository)

    assert any(
        finding.check == "verdict-metadata-mismatch" and finding.level == "violation"
        for finding in findings
    )


@pytest.mark.parametrize(
    ("application", "posting"),
    [
        (ApplicationStatus.PENDING, PostingStatus.CLOSED),
        (ApplicationStatus.APPLIED, PostingStatus.ACTIVE),
        (ApplicationStatus.REJECTED, PostingStatus.ACTIVE),
    ],
)
def test_closed_and_protected_application_states_exempt_verdict_mismatch(
    tmp_path: Path,
    application: ApplicationStatus,
    posting: PostingStatus,
) -> None:
    repository, key = _stored(
        tmp_path / "records",
        verdict=ScreeningVerdict.RECOMMENDED,
        application=application,
        posting=posting,
        body_verdict="지원 비추천",
    )

    checks = [finding.check for finding in screening_lint.lint_record(key, repository)]

    assert "verdict-metadata-mismatch" not in checks


def test_key_from_screening_path_and_corrupt_record_detail_are_sanitized(tmp_path: Path) -> None:
    records_root = tmp_path / "records"
    screening = records_root / "wanted/999/content/rev-1/screening.md"
    screening.parent.mkdir(parents=True)
    screening.write_text(_screening("지원 추천"), encoding="utf-8")

    assert screening_lint.key_from_screening_path(screening, records_root) == JobKey("wanted", "999")

    valid_repository, key = _stored(records_root)
    (records_root / key.platform / key.job_id / "record.json").write_text("{broken", encoding="utf-8")
    corrupt = screening_lint.lint_record(key, valid_repository)
    assert any(f.check == "storage-corrupt" and f.level == "violation" for f in corrupt)
    assert str(records_root) not in corrupt[0].detail


def test_existing_headhunter_and_crypto_warning_semantics_are_preserved(tmp_path: Path) -> None:
    repository, key = _stored(
        tmp_path / "records",
        verdict=ScreeningVerdict.NOT_RECOMMENDED,
        body_verdict="지원 비추천",
        jd="# JD\n일반 정규직",
    )
    repository.update_screening_result(
        key,
        screening_markdown=_screening("지원 비추천", "| 회사 유형 | ❌ | 고용 알선업(헤드헌팅/써치펌) |"),
    )
    findings = screening_lint.lint_record(key, repository)
    assert any(f.check == "headhunter-hard-cut" and f.level == "violation" for f in findings)


def test_hook_targets_select_only_canonical_screening_file_for_apply_patch(tmp_path: Path) -> None:
    records_root = tmp_path / "records"
    screening = records_root / "wanted/123/content/rev/screening.md"
    screening.parent.mkdir(parents=True)
    screening.write_text("screening", encoding="utf-8")
    jd = screening.with_name("jd.md")
    jd.write_text("jd", encoding="utf-8")
    outside = tmp_path / "private.md"
    outside.write_text("private", encoding="utf-8")
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {
            "command": "\n".join(
                f"*** Update File: {path.relative_to(tmp_path)}"
                for path in (screening, jd, outside)
            )
        },
    }

    assert screening_lint.hook_target_paths(payload=payload, records_root=records_root) == [screening.resolve()]


@pytest.mark.parametrize("tool_name", ["Write", "Edit"])
def test_hook_target_parity_for_claude_write_edit_and_codex_apply_patch(
    tmp_path: Path,
    tool_name: str,
) -> None:
    records_root = tmp_path / "records"
    screening = records_root / "wanted/123/content/rev/screening.md"
    screening.parent.mkdir(parents=True)
    screening.write_text("screening", encoding="utf-8")

    write_payload = {
        "cwd": str(tmp_path),
        "tool_name": tool_name,
        "tool_input": {"file_path": str(screening.relative_to(tmp_path))},
    }
    patch_payload = {
        "cwd": str(tmp_path),
        "tool_name": "apply_patch",
        "tool_input": {"command": f"*** Update File: {screening.relative_to(tmp_path)}\n"},
    }

    write_keys = screening_lint.hook_keys_from_stdin(io.StringIO(json.dumps(write_payload)), records_root=records_root)
    patch_keys = screening_lint.hook_keys_from_stdin(io.StringIO(json.dumps(patch_payload)), records_root=records_root)

    assert write_keys == patch_keys == [JobKey("wanted", "123")]


def test_render_report_exit_status_is_deterministic(tmp_path: Path) -> None:
    repository, key = _stored(
        tmp_path / "records",
        verdict=ScreeningVerdict.RECOMMENDED,
        body_verdict="지원 비추천",
    )

    report = screening_lint.run([key], repository)

    exit_code = screening_lint.render_report(report, stdout=io.StringIO(), stderr=io.StringIO())

    assert exit_code == 2
    assert report.summary() == "검사 1건: 위반 1, 경고 0"


def test_legacy_four_column_screening_without_provenance_metadata_remains_lint_clean(
    tmp_path: Path,
) -> None:
    repository, key = _stored(tmp_path / "records")
    repository.update_screening_result(
        key,
        screening_markdown="""## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | Acme |
| 포지션 | Backend |

## 스크리닝 결과

- 구조화된 4열 문서

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|------|------|------|------|
| Spring Boot 개발 | 필수 | 충족 | 사내 백엔드 개발 경험 |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 기존 4열 계약 유지
""",
    )

    assert screening_lint.lint_record(key, repository) == []


def test_fallback_two_column_screening_document_remains_lint_clean(tmp_path: Path) -> None:
    repository, key = _stored(
        tmp_path / "records",
        verdict=ScreeningVerdict.HOLD,
        body_verdict="지원 보류",
        jd="# JD\n",
    )
    stored = repository.get(key)
    repository.update_screening_result(
        key,
        screening_markdown=build_fallback_output(
            stored,
            stored.jd_markdown,
            "structured contract retry failed",
        ),
    )

    assert screening_lint.lint_record(key, repository) == []
