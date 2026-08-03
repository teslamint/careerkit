from __future__ import annotations

import base64
import json
import logging
import os
import textwrap
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from careerkit.jobs import cli
from careerkit.jobs.adapters.screening.cli_provider import FakeProvider
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, StoredJobRecord
from careerkit.jobs.application.automation import (
    AutomationService,
    CompletionBatch,
    ExtractionBatch,
    JobsCompletionStage,
    JobsExtractionStage,
    JobsAutoResultService,
    JobsResumeStateService,
    JobsScreeningStage,
    ScreeningBatch,
    _COMPANY_INFO_MISSING,
    _COMPANY_INFO_INCOMPLETE,
    _render_json,
)
from careerkit.jobs.application.company_info import CompanyInfoService, CompanyValidationSummary
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.requirement_manifest import RequirementKind, extract_requirement_manifest
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.application.search import SearchCandidate, SearchResult
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict
from careerkit.workspace import resolve_workspace


GOLDEN_LLM_OUTPUT = """## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | GoldenCo |
| 포지션 | Senior Backend Engineer |

## 스크리닝 결과

백엔드 운영 경험과 JD 요구사항이 일치한다.

## 이력/경험 매칭

| 요건 | 구분 | 대조 | 근거 |
|------|------|------|------|
| Backend 서비스 개발 | 필수 | 충족 | 고정 후보자 컨텍스트 |

## 최종 판정

### 최종 판정: 지원 추천

## 핵심 근거

- 운영 안정성 경험과 역할 요구가 맞는다.
- 기술 범위가 백엔드 중심이다.
"""

GOLDEN_ASSESSMENT_JSON = json.dumps(
    {
        "schema_version": 1,
        "matches": [
            {
                "id": "required-001",
                "match": "충족",
                "evidence": "[source: synthetic/profile.md] Python 근거",
            },
            {
                "id": "required-002",
                "match": "충족",
                "evidence": "[source: synthetic/profile.md] API 근거",
            },
            {
                "id": "preferred-001",
                "match": "충족",
                "evidence": "[source: synthetic/profile.md] 검색 경험 근거",
            },
        ],
        "verdict": "지원 추천",
        "decision_basis": [],
        "screening_summary": ["요건 기반 구조화 평가를 완료했다"],
        "reasons": [
            "후보자 이력의 명시 근거만 사용했다",
            "source-owned requirement manifest를 그대로 따랐다",
            "최종 판정은 구조화된 대조 결과로 작성했다",
        ],
    },
    ensure_ascii=False,
)


@dataclass
class FakeSearchPort:
    result: SearchResult
    persisted: list[set[str]] = field(default_factory=list)
    calls: int = 0
    max_urls: list[int | None] = field(default_factory=list)

    def search(self, *, max_urls: int | None = None) -> SearchResult:
        self.calls += 1
        self.max_urls.append(max_urls)
        return self.result

    def persist_seen_job_keys(
        self, seen_job_keys: set[str], *, new_count: int | None = None
    ) -> None:
        self.persisted.append(set(seen_job_keys))


@dataclass
class FakeExtractionStage:
    calls: list[tuple[tuple[str, ...], bool, bool]] = field(default_factory=list)

    def extract(
        self,
        urls: Sequence[str],
        *,
        dry_run: bool,
        screening_only: bool,
    ) -> ExtractionBatch:
        batch = tuple(urls)
        records = tuple(
            StoredJobRecord(
                record=JobRecord("wanted", str(idx), "Acme", f"Role {idx}", source_url=url),
                jd_markdown=f"# {url}\n",
            )
            for idx, url in enumerate(batch, start=1)
        )
        self.calls.append((batch, dry_run, screening_only))
        return ExtractionBatch(
            urls=batch,
            item_ids=tuple(f"wanted:{idx}" for idx, _ in enumerate(batch, start=1)),
            records=records,
            metadata={"source": "extract"},
        )


@dataclass
class FakeScreeningStage:
    calls: list[tuple[tuple[str, ...], bool, int, int | None]] = field(
        default_factory=list
    )

    def screen(
        self,
        extraction: ExtractionBatch,
        *,
        dry_run: bool,
        llm_timeout: int,
        local_llm_timeout: int | None = None,
    ) -> ScreeningBatch:
        self.calls.append((tuple(extraction.item_ids), dry_run, llm_timeout, local_llm_timeout))
        return ScreeningBatch(item_ids=tuple(extraction.item_ids), metadata={"source": "screen"})


@dataclass
class FakeCompletionStage:
    calls: list[tuple[tuple[str, ...], tuple[str, ...], bool, bool]] = field(default_factory=list)

    def complete(
        self,
        extraction: ExtractionBatch,
        screening: ScreeningBatch,
        *,
        dry_run: bool,
        no_classify: bool,
    ) -> CompletionBatch:
        self.calls.append((tuple(extraction.item_ids), tuple(screening.item_ids), dry_run, no_classify))
        return CompletionBatch(item_ids=tuple(screening.item_ids), metadata={"source": "complete"})


@dataclass
class FakeResumeState:
    pending_urls: tuple[str, ...] = ()
    saved: list[tuple[str, ...]] = field(default_factory=list)
    cleared: int = 0

    def load_pending_urls(self) -> tuple[str, ...]:
        return self.pending_urls

    def save_pending_urls(self, urls: Sequence[str]) -> None:
        self.saved.append(tuple(urls))

    def clear_pending_urls(self) -> None:
        self.cleared += 1


class FakeHttpClient:
    def __init__(self, *, text_by_url: dict[str, str] | None = None, json_by_url: dict[str, dict[str, Any]] | None = None) -> None:
        self.text_by_url = dict(text_by_url or {})
        self.json_by_url = dict(json_by_url or {})
        self.requested_text: list[str] = []
        self.requested_json: list[str] = []

    def request_text(self, url: str, **_: Any) -> str:
        self.requested_text.append(url)
        if url not in self.text_by_url:
            raise AssertionError(f"unexpected request_text: {url}")
        return self.text_by_url[url]

    def request_json(self, url: str, **_: Any) -> dict[str, Any]:
        self.requested_json.append(url)
        if url not in self.json_by_url:
            raise AssertionError(f"unexpected request_json: {url}")
        return self.json_by_url[url]


def _search_result() -> SearchResult:
    return SearchResult(
        postings=(
            SearchCandidate("wanted", "1", "wanted:1", "Backend", "Acme", "3년", "https://wanted.example/1"),
            SearchCandidate("remember", "2", "remember:2", "Platform", "Beta", "5년", "https://remember.example/2"),
        ),
        updated_seen_job_keys={"wanted:1"},
        total_found=5,
        filtered_out=2,
        duplicates=1,
        diagnostics=("semantic filter unavailable",),
        capabilities={"semantic": {"available": False, "reason": "missing model"}},
    )


def _make_workspace(tmp_path: Path):
    (tmp_path / ".career-workspace").write_text("1", encoding="utf-8")
    (tmp_path / "private/jd/config").mkdir(parents=True)
    (tmp_path / "private/jd/config/jd-screening-rules.md").write_text(
        "# Rules\n- backend",
        encoding="utf-8",
    )
    return resolve_workspace(explicit=tmp_path)


def _write_valid_company_info(tmp_path: Path, slug: str, name: str) -> Path:
    path = tmp_path / "private/company_info" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {name}\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n| 직원수 | 100명 |\n| 업종 | IT |\n\n"
        "## 연봉 정보\n\n"
        "| 항목 | 금액 |\n|------|------|\n| 평균 연봉 | **5,000만원** |\n",
        encoding="utf-8",
    )
    return path


def _wanted_html(job_id: str) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "position": "Senior Backend Engineer",
                    "company": {"company_name": "GoldenCo"},
                    "career": {"annual_from": 3, "annual_to": 7},
                    "address": {"full_location": "Seoul"},
                    "intro": "서비스 소개",
                    "main_tasks": "백엔드 시스템 개발",
                    "requirements": "Python, API",
                    "preferred_points": "검색 경험",
                    "benefits": "점심 제공",
                }
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></html>'


def _remember_html() -> str:
    posting = {
        "title": "Backend Engineer",
        "organization": {"name": "Remember Co"},
        "introduction": "회사와 제품 소개",
        "jobDescription": "백엔드 서비스 개발",
        "qualifications": "Python",
        "recruitingProcess": "서류 > 인터뷰",
        "additionalInformation": "장비 지원",
        "desiredProfileCondition": {"skills": [{"name": "FastAPI"}]},
        "chips": [{"value": "누적 투자 100억"}],
        "classifiedTags": [{"value": "재택근무"}],
        "leaderPosition": True,
        "jobRankCategory": "팀장",
    }
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": {"data": posting}}}]
                }
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></html>'


def _groupby_payload(*, task: str, qualification: str, preferred: str, brief_intro: str = "B2B 데이터 플랫폼") -> dict[str, Any]:
    return {
        "status": 200,
        "data": {
            "name": "Backend Engineer",
            "careerType": "경력",
            "experienceRange": {"min": 4, "max": 8},
            "task": task,
            "qualification": qualification,
            "preferred": preferred,
            "startup": {
                "name": "Group Co",
                "briefIntro": brief_intro,
                "memberCount": 40,
                "devCount": 12,
                "fundingRound": "Series A",
                "serviceAreas": ["SaaS", "Data"],
            },
        },
    }


def _saramin_detail_html(
    job_id: str,
    *,
    intro: str = "플랫폼 소개 문단",
    detail_requirements: str = "",
    detail_preferred: str = "",
    jd_body: str = "",
) -> str:
    encoded_body = base64.b64encode(jd_body.encode("utf-8")).decode("ascii")
    return textwrap.dedent(f"""\
        <html>
        <head>
            <title>[마감전] Backend Engineer (D-3) - 사람인</title>
            <meta name="description" content="GoldenCo, Backend Engineer, 경력:경력 3~7년, 학력:무관">
        </head>
        <body>
            <span class="corp_name">GoldenCo</span>
            <dl>
                <dt class="tit">지역</dt>
                <dd class="desc">서울</dd>
                <dt class="tit">경력</dt>
                <dd class="desc">경력 3~7년</dd>
                <dt class="tit">근무형태</dt>
                <dd class="desc">정규직</dd>
                <dt class="tit">급여</dt>
                <dd class="desc">면접 후 결정</dd>
                <dt class="tit">자격요건</dt>
                <dd class="desc">{detail_requirements}</dd>
                <dt class="tit">우대사항</dt>
                <dd class="desc">{detail_preferred}</dd>
                <dt class="tit">급여제도</dt>
                <dd class="desc">성과급</dd>
            </dl>
            <script>
                var detailContents_{job_id} = {{
                    contents: '{encoded_body}',
                    mobile_contents_yn: ''
                }};
            </script>
            <div>{intro}</div>
        </body>
        </html>
    """)


def _saramin_text_by_url(url: str, html: str) -> dict[str, str]:
    job_id = url.rsplit("=", 1)[-1]
    return {
        f"https://m.saramin.co.kr/job-search/view?rec_idx={job_id}": html,
    }


def _section_body(markdown: str, heading: str) -> str:
    marker = f"## {heading}\n\n"
    start = markdown.index(marker) + len(marker)
    end = markdown.find("\n## ", start)
    if end == -1:
        end = len(markdown)
    return markdown[start:end].strip()


def _manifest_json_from_prompt(prompt: str) -> str:
    marker = "[source-owned requirement manifest]\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n\n[JD 원문]\n", start)
    return prompt[start:end]


class CapturingFakeProvider(FakeProvider):
    def __init__(self, output: str, provider_name: str) -> None:
        super().__init__(output, provider_name=provider_name)
        self.prompts: list[str] = []

    def run(
        self,
        prompt: str,
        timeout: int,
        local_timeout: int | None = None,
    ) -> tuple[str, str]:
        self.prompts.append(prompt)
        return super().run(prompt, timeout)


def test_run_auto_search_only_persists_final_seen_keys() -> None:
    search_port = FakeSearchPort(_search_result())
    service = AutomationService(search_port=search_port)

    result = service.run("auto", ["--search-only", "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "search"
    assert payload["counts"] == {
        "total_found": 5,
        "returned": 2,
        "filtered_out": 2,
        "duplicates": 1,
    }
    assert search_port.persisted == [{"wanted:1"}]
    assert search_port.calls == 1


def test_run_auto_search_only_writes_request_file_for_handoff(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        result_store=JobsAutoResultService(workspace=workspace),
    )

    result = service.run("auto", ["--search-only", "--json"])

    payload = json.loads(result.stdout)
    request_path = tmp_path / payload["request_path"]
    assert request_path.read_text(encoding="utf-8").splitlines() == [
        "https://wanted.example/1",
        "https://remember.example/2",
    ]


def test_run_auto_dry_run_does_not_persist_seen_keys() -> None:
    search_port = FakeSearchPort(_search_result())
    service = AutomationService(search_port=search_port)

    result = service.run("auto", ["--search-only", "--dry-run", "--json"])

    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "search"
    assert search_port.persisted == []


def test_run_auto_requires_extraction_stage_for_non_search_flow() -> None:
    service = AutomationService(search_port=FakeSearchPort(_search_result()))

    result = service.run("auto", ["--json"])

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "career-jobs run auto requires an extraction stage port."


def test_run_auto_requires_screening_and_completion_stages_for_full_flow() -> None:
    extraction = FakeExtractionStage()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=extraction,
    )

    screening_result = service.run("auto", ["--json"])
    assert screening_result.returncode == 2
    assert screening_result.stderr == "career-jobs run auto requires a screening stage port."

    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=extraction,
        screening_stage=FakeScreeningStage(),
    )
    completion_result = service.run("auto", ["--json"])
    assert completion_result.returncode == 2
    assert completion_result.stderr == "career-jobs run auto requires a completion stage port for classification/status/summary."


def test_run_auto_full_pipeline_runs_search_extract_screen_complete_and_clears_resume_state() -> None:
    search_port = FakeSearchPort(_search_result())
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    completion = FakeCompletionStage()
    resume_state = FakeResumeState()
    service = AutomationService(
        search_port=search_port,
        extraction_stage=extraction,
        screening_stage=screening,
        completion_stage=completion,
        resume_state=resume_state,
    )

    result = service.run("auto", ["--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "search"
    assert payload["stage"] == "complete"
    assert payload["completed_count"] == 2
    assert extraction.calls == [(("https://wanted.example/1", "https://remember.example/2"), False, False)]
    assert screening.calls == [(("wanted:1", "wanted:2"), False, 120, None)]
    assert completion.calls == [(("wanted:1", "wanted:2"), ("wanted:1", "wanted:2"), False, False)]
    assert resume_state.saved == [("https://wanted.example/1", "https://remember.example/2")]
    assert resume_state.cleared == 1


def test_fresh_dry_run_stops_after_extraction() -> None:
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=extraction,
        screening_stage=screening,
    )

    result = service.run("auto", ["--dry-run", "--json"])

    assert result.returncode == 0
    assert json.loads(result.stdout)["stage"] == "extract"
    assert screening.calls == []


def test_resume_state_loads_migrated_pending_items(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    state_dir = tmp_path / "private/jd/runtime/auto/state"
    state_dir.mkdir(parents=True)
    stale = state_dir / ".auto_state_zz-stale.json"
    stale.write_text(
        json.dumps({"items": {"wanted:9": {"status": "pending", "url": "https://wanted/9"}}}),
        encoding="utf-8",
    )
    latest = state_dir / ".auto_state_20260101.json"
    latest.write_text(
        json.dumps({"items": {"wanted:1": {"status": "failed", "url": "https://wanted/1"}}}),
        encoding="utf-8",
    )
    os.utime(stale, (1, 1))
    os.utime(latest, (2, 2))

    assert JobsResumeStateService(workspace=workspace).load_pending_urls() == (
        "https://wanted/1",
    )


def test_run_auto_from_urls_uses_injected_stages_without_search(tmp_path: Path) -> None:
    url_file = tmp_path / "urls.txt"
    url_file.write_text(
        "https://wanted.example/1\n# keep comment\nhttps://remember.example/2\n",
        encoding="utf-8",
    )
    search_port = FakeSearchPort(_search_result())
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    completion = FakeCompletionStage()
    service = AutomationService(
        search_port=search_port,
        extraction_stage=extraction,
        screening_stage=screening,
        completion_stage=completion,
    )

    result = service.run("auto", ["--from-urls", str(url_file), "--screening-only", "--dry-run", "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "from_urls"
    assert payload["stage"] == "complete"
    assert payload["url_count"] == 2
    assert payload["extracted_count"] == 2
    assert payload["screened_count"] == 2
    assert payload["completed_count"] == 2
    assert extraction.calls == [(("https://wanted.example/1", "https://remember.example/2"), True, True)]
    assert screening.calls == [(("wanted:1", "wanted:2"), True, 120, None)]
    assert completion.calls == [(("wanted:1", "wanted:2"), ("wanted:1", "wanted:2"), True, False)]
    assert search_port.calls == 0
    assert search_port.persisted == []


def test_run_auto_resume_uses_saved_urls_without_search() -> None:
    search_port = FakeSearchPort(_search_result())
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    completion = FakeCompletionStage()
    resume_state = FakeResumeState(
        pending_urls=("https://wanted.example/9", "https://remember.example/8")
    )
    service = AutomationService(
        search_port=search_port,
        extraction_stage=extraction,
        screening_stage=screening,
        completion_stage=completion,
        resume_state=resume_state,
    )

    result = service.run("auto", ["--resume", "--dry-run", "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "resume"
    assert payload["url_count"] == 2
    assert extraction.calls == [(("https://wanted.example/9", "https://remember.example/8"), True, False)]
    assert search_port.calls == 0
    assert resume_state.saved == []
    assert resume_state.cleared == 0


def test_run_auto_resume_with_cap_preserves_unprocessed_tail() -> None:
    resume_state = FakeResumeState(
        pending_urls=(
            "https://wanted.example/1",
            "https://wanted.example/2",
            "https://wanted.example/3",
        )
    )
    extraction = FakeExtractionStage()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=extraction,
        screening_stage=FakeScreeningStage(),
        completion_stage=FakeCompletionStage(),
        resume_state=resume_state,
    )

    result = service.run("auto", ["--resume", "--max-urls", "2", "--json"])

    assert result.returncode == 0
    assert extraction.calls[0][0] == (
        "https://wanted.example/1",
        "https://wanted.example/2",
    )
    assert resume_state.saved[-1] == ("https://wanted.example/3",)
    assert resume_state.cleared == 0


def test_run_auto_applies_max_urls_timeout_and_no_classify() -> None:
    result = replace(
        _search_result(),
        updated_seen_job_keys={"existing:9", "wanted:1", "remember:2"},
    )
    search_port = FakeSearchPort(result)
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    completion = FakeCompletionStage()
    service = AutomationService(
        search_port=search_port,
        extraction_stage=extraction,
        screening_stage=screening,
        completion_stage=completion,
    )

    run = service.run(
        "auto",
        ["--max-urls", "1", "--llm-timeout", "45", "--no-classify", "--json"],
    )

    assert run.returncode == 0
    assert extraction.calls == [(("https://wanted.example/1",), False, False)]
    assert screening.calls == [(("wanted:1",), False, 45, None)]
    assert completion.calls == [(("wanted:1",), ("wanted:1",), False, True)]
    assert search_port.persisted == [{"existing:9", "wanted:1"}]
    assert search_port.max_urls == [1]


def test_run_auto_forwards_local_llm_timeout_to_screening() -> None:
    result = replace(
        _search_result(),
        updated_seen_job_keys={"existing:9", "wanted:1", "remember:2"},
    )
    search_port = FakeSearchPort(result)
    extraction = FakeExtractionStage()
    screening = FakeScreeningStage()
    completion = FakeCompletionStage()
    service = AutomationService(
        search_port=search_port,
        extraction_stage=extraction,
        screening_stage=screening,
        completion_stage=completion,
    )

    run = service.run(
        "auto",
        ["--llm-timeout", "45", "--local-llm-timeout", "300", "--json"],
    )

    assert run.returncode == 0
    assert screening.calls[0][2] == 45
    assert screening.calls[0][3] == 300


def test_run_auto_resume_requires_state_port() -> None:
    service = AutomationService(search_port=FakeSearchPort(_search_result()))

    result = service.run("auto", ["--resume", "--json"])

    assert result.returncode == 2
    assert result.stderr == "career-jobs run auto --resume requires a resume state port."


def test_jobs_extraction_stage_screening_only_reuses_existing_record_without_fetch(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="123456",
            company="Acme",
            position="Backend",
            source_url="https://www.wanted.co.kr/wd/123456",
        ),
        jd_markdown="# JD\n",
    )
    http_client = FakeHttpClient()
    stage = JobsExtractionStage(repository=repository, http_client=http_client)

    batch = stage.extract(
        ["https://www.wanted.co.kr/wd/123456"],
        dry_run=True,
        screening_only=True,
    )

    assert batch.item_ids == ("wanted:123456",)
    assert batch.metadata["reused_existing_records"] is True
    assert batch.metadata["items"] == [
        {"job_key": "wanted:123456", "company": "Acme", "position": "Backend"}
    ]
    assert http_client.requested_text == []
    assert batch.records[0].record.company == "Acme"


def test_jobs_extraction_stage_logs_each_url_at_debug(tmp_path: Path, caplog) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="123456",
            company="Acme",
            position="Backend",
            source_url="https://www.wanted.co.kr/wd/123456",
        ),
        jd_markdown="# JD\n",
    )
    stage = JobsExtractionStage(repository=repository, http_client=FakeHttpClient())
    caplog.set_level(logging.DEBUG, logger="careerkit.jobs.application.automation")

    stage.extract(
        ["https://www.wanted.co.kr/wd/123456"],
        dry_run=True,
        screening_only=True,
    )

    assert "extracting: https://www.wanted.co.kr/wd/123456" in caplog.messages


def test_jobs_extraction_stage_skips_existing_record_in_normal_mode(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    repository.create(
        JobRecord("wanted", "123456", "Acme", "Curated Backend"),
        jd_markdown="# Curated JD\n",
    )
    http_client = FakeHttpClient()
    stage = JobsExtractionStage(repository=repository, http_client=http_client)

    batch = stage.extract(
        ["https://www.wanted.co.kr/wd/123456"],
        dry_run=False,
        screening_only=False,
    )

    assert batch.item_ids == ()
    assert batch.metadata["duplicates"] == ["wanted:123456"]
    assert batch.metadata["duplicate_count"] == 1
    assert http_client.requested_text == []
    assert repository.get(JobKey("wanted", "123456")).jd_markdown == "# Curated JD\n"


def test_jobs_extraction_stage_continues_after_bad_url_and_reports_failure(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    valid_url = "https://www.wanted.co.kr/wd/123456"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(text_by_url={valid_url: _wanted_html("123456")}),
    )

    batch = stage.extract(
        ["https://unsupported.example/jobs/1", valid_url],
        dry_run=False,
        screening_only=False,
    )

    assert batch.item_ids == ("wanted:123456",)
    assert batch.urls == (valid_url,)
    assert batch.failed_urls == ("https://unsupported.example/jobs/1",)
    assert batch.metadata["failure_count"] == 1
    assert batch.metadata["failures"][0]["item_id"] == "item-1"
    assert "unsupported.example" not in batch.metadata["failures"][0]["error"]


def test_extraction_metadata_includes_title_items(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    wanted_url = "https://www.wanted.co.kr/wd/123456"
    remember_url = "https://career.rememberapp.co.kr/job/posting/123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                wanted_url: _wanted_html("123456"),
                remember_url: _remember_html(),
            }
        ),
    )

    batch = stage.extract(
        [wanted_url, "https://unsupported.example/jobs/1", remember_url],
        dry_run=True,
        screening_only=False,
    )

    assert batch.metadata["items"] == [
        {"job_key": "wanted:123456", "company": "GoldenCo", "position": "Senior Backend Engineer"},
        {"job_key": "remember:123", "company": "Remember Co", "position": "Backend Engineer"},
    ]
    assert [item["job_key"] for item in batch.metadata["items"]] == list(batch.item_ids)
    assert batch.metadata["failure_count"] == 1


def test_render_human_lists_new_posting_titles() -> None:
    payload = {
        "mode": "from_urls",
        "url_count": 2,
        "stage": "extract",
        "extraction": {
            "mode": "extract",
            "failure_count": 0,
            "items": [
                {"job_key": "wanted:1", "company": "GoldenCo", "position": "Senior Backend Engineer"},
                {"job_key": "remember:2", "company": "Remember Co", "position": "Backend Engineer"},
            ],
        },
    }

    output = _render_json(payload, False)

    lines = output.splitlines()
    assert "new: GoldenCo — Senior Backend Engineer (wanted:1)" in lines
    assert "new: Remember Co — Backend Engineer (remember:2)" in lines
    extraction_line = next(line for line in lines if line.startswith("extraction="))
    assert "items" not in extraction_line
    assert "failure_count" in extraction_line
    assert _render_json({"mode": "search", "url_count": 2}, False) == "mode=search\nurl_count=2\n"


def test_render_human_uses_item_prefix_for_screening_only() -> None:
    payload = {
        "stage": "extract",
        "extraction": {
            "mode": "screening_only",
            "items": [
                {"job_key": "wanted:1", "company": "GoldenCo", "position": "Senior Backend Engineer"},
            ],
        },
    }

    output = _render_json(payload, False)

    assert "item: GoldenCo — Senior Backend Engineer (wanted:1)" in output.splitlines()
    assert "new:" not in output


def test_jobs_extraction_stage_wanted_separates_intro_and_normalizes_explicit_bullets(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.wanted.co.kr/wd/123456"
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "position": "Senior Backend Engineer",
                    "company": {"company_name": "GoldenCo"},
                    "career": {"annual_from": 3, "annual_to": 7},
                    "address": {"full_location": "Seoul"},
                    "intro": "• 소개 bullet은 유지",
                    "main_tasks": "• API 개발\n•데이터 파이프라인 운영\n  • 장애 대응",
                    "requirements": "• Python\n•FastAPI\n  • 테스트 자동화",
                    "preferred_points": "• 검색 경험\n•협업 경험",
                    "benefits": "점심 제공",
                }
            }
        }
    }
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                url: f'<html><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></html>'
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "포지션 소개") == "• 소개 bullet은 유지"
    assert _section_body(markdown, "주요 업무") == "- API 개발\n- 데이터 파이프라인 운영\n  - 장애 대응"
    assert _section_body(markdown, "자격 요건") == "- Python\n- FastAPI\n  - 테스트 자동화"
    assert _section_body(markdown, "우대사항") == "- 검색 경험\n- 협업 경험"

    manifest = extract_requirement_manifest(markdown)

    assert "소개 bullet은 유지" not in [item.text for item in manifest.parents]
    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("API 개발", RequirementKind.MAIN_DUTY),
        ("데이터 파이프라인 운영", RequirementKind.MAIN_DUTY),
        ("Python", RequirementKind.REQUIRED),
        ("FastAPI", RequirementKind.REQUIRED),
        ("검색 경험", RequirementKind.PREFERRED),
        ("협업 경험", RequirementKind.PREFERRED),
    ]


def test_jobs_extraction_stage_remember_separates_intro_and_keeps_plain_requirements_ambiguous(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://career.rememberapp.co.kr/job/posting/123"
    posting = {
        "title": "Backend Engineer",
        "organization": {"name": "Remember Co"},
        "introduction": "제품 소개 문단",
        "jobDescription": "• API 서버 개발\n•장애 대응",
        "qualifications": "필수: 빠르게 배우고 협업할 수 있는 분",
        "preferredQualifications": "• FastAPI 경험",
    }
    payload = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": {"data": posting}}}]
                }
            }
        }
    }
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(text_by_url={url: f'<html><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></html>'}),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "포지션 소개") == "제품 소개 문단"
    assert _section_body(markdown, "주요 업무") == "- API 서버 개발\n- 장애 대응"
    manifest = extract_requirement_manifest(markdown)

    assert [item.text for item in manifest.parents] == ["API 서버 개발", "장애 대응", "FastAPI 경험"]
    assert [item.kind for item in manifest.parents] == [
        RequirementKind.MAIN_DUTY,
        RequirementKind.MAIN_DUTY,
        RequirementKind.PREFERRED,
    ]
    assert manifest.ambiguous_qualifications is True


def test_remember_extraction_preserves_all_operator_visible_fields(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://career.rememberapp.co.kr/job/posting/123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(text_by_url={url: _remember_html()}),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    for expected in (
        "회사와 제품 소개",
        "백엔드 서비스 개발",
        "서류 > 인터뷰",
        "장비 지원",
        "FastAPI",
        "누적 투자 100억",
        "재택근무",
        "팀장",
        "리더 포지션",
    ):
        assert expected in markdown


def test_jobs_extraction_stage_groupby_separates_company_context_and_task_sections(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://groupby.kr/positions/456"
    api_url = "https://api.groupby.kr/startup-positions/456"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            json_by_url={
                api_url: _groupby_payload(
                    task="<p>• 서비스 개발</p><p>  •장애 대응</p>",
                    qualification="<p>정보 없음</p>",
                    preferred="<p>• GraphQL 경험</p>",
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    intro = _section_body(markdown, "포지션 소개")
    assert "B2B 데이터 플랫폼" in intro
    assert "서비스 개발" not in intro
    assert _section_body(markdown, "주요 업무") == "- 서비스 개발\n  - 장애 대응"
    assert _section_body(markdown, "자격 요건") == "정보 없음"
    assert _section_body(markdown, "우대사항") == "- GraphQL 경험"

    manifest = extract_requirement_manifest(markdown)

    assert [item.text for item in manifest.parents] == ["서비스 개발", "GraphQL 경험"]
    assert [item.kind for item in manifest.parents] == [
        RequirementKind.MAIN_DUTY,
        RequirementKind.PREFERRED,
    ]
    assert manifest.ambiguous_qualifications is False


def test_saramin_extraction_renders_detail_field_manifest_rows(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616301"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616301",
                    detail_requirements="<ul><li>Python 백엔드 개발 경험</li><li>SQL 활용 능력</li></ul>",
                    detail_preferred="<p>테스트 코드 작성 경험</p><p>Docker 운영 경험</p>",
                    jd_body="회사 소개\n안정적인 SaaS 운영",
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "자격 요건") == "- Python 백엔드 개발 경험\n- SQL 활용 능력"
    assert _section_body(markdown, "우대사항") == "- 테스트 코드 작성 경험\n- Docker 운영 경험"

    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("Python 백엔드 개발 경험", RequirementKind.REQUIRED),
        ("SQL 활용 능력", RequirementKind.REQUIRED),
        ("테스트 코드 작성 경험", RequirementKind.PREFERRED),
        ("Docker 운영 경험", RequirementKind.PREFERRED),
    ]


def test_saramin_extraction_canonicalizes_mixed_detail_requirement_lines(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616307"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616307",
                    detail_requirements="<ul><li>Python 백엔드 개발 경험</li></ul><p>SQL 활용 능력</p><br>문제 해결 능력",
                    jd_body="회사 소개\n안정적인 SaaS 운영",
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "자격 요건") == (
        "- Python 백엔드 개발 경험\n- SQL 활용 능력\n- 문제 해결 능력"
    )

    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("Python 백엔드 개발 경험", RequirementKind.REQUIRED),
        ("SQL 활용 능력", RequirementKind.REQUIRED),
        ("문제 해결 능력", RequirementKind.REQUIRED),
    ]


@pytest.mark.parametrize(
    (
        "detail_requirements",
        "detail_preferred",
        "jd_body",
        "expected_requirements",
        "expected_preferred",
        "expected_manifest",
    ),
    [
        (
            "",
            "<p>Docker 운영 경험</p>",
            "자격요건\nPython 백엔드 개발 경험\nSQL 활용 능력",
            "- Python 백엔드 개발 경험\n- SQL 활용 능력",
            "- Docker 운영 경험",
            [
                ("Python 백엔드 개발 경험", RequirementKind.REQUIRED),
                ("SQL 활용 능력", RequirementKind.REQUIRED),
                ("Docker 운영 경험", RequirementKind.PREFERRED),
            ],
        ),
        (
            "<ul><li>Python 백엔드 개발 경험</li></ul>",
            "",
            "우대사항\n테스트 코드 작성 경험\nDocker 운영 경험",
            "- Python 백엔드 개발 경험",
            "- 테스트 코드 작성 경험\n- Docker 운영 경험",
            [
                ("Python 백엔드 개발 경험", RequirementKind.REQUIRED),
                ("테스트 코드 작성 경험", RequirementKind.PREFERRED),
                ("Docker 운영 경험", RequirementKind.PREFERRED),
            ],
        ),
    ],
)
def test_saramin_extraction_uses_body_sections_per_missing_field(
    tmp_path: Path,
    detail_requirements: str,
    detail_preferred: str,
    jd_body: str,
    expected_requirements: str,
    expected_preferred: str,
    expected_manifest: list[tuple[str, RequirementKind]],
) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616302"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616302",
                    detail_requirements=detail_requirements,
                    detail_preferred=detail_preferred,
                    jd_body=jd_body,
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "자격 요건") == expected_requirements
    assert _section_body(markdown, "우대사항") == expected_preferred

    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == expected_manifest


@pytest.mark.parametrize(
    ("detail_requirements", "detail_preferred", "jd_body", "expected_manifest"),
    [
        (
            "<ul><li>상세 자격 우선</li></ul>",
            "",
            "자격요건\n본문 자격 대체 금지\n우대사항\n본문 우대 허용",
            [
                ("상세 자격 우선", RequirementKind.REQUIRED),
                ("본문 우대 허용", RequirementKind.PREFERRED),
            ],
        ),
        (
            "",
            "<p>상세 우대 우선</p>",
            "자격요건\n본문 자격 허용\n우대사항\n본문 우대 대체 금지",
            [
                ("본문 자격 허용", RequirementKind.REQUIRED),
                ("상세 우대 우선", RequirementKind.PREFERRED),
            ],
        ),
    ],
)
def test_saramin_extraction_applies_field_precedence_per_section(
    tmp_path: Path,
    detail_requirements: str,
    detail_preferred: str,
    jd_body: str,
    expected_manifest: list[tuple[str, RequirementKind]],
) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616303"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616303",
                    detail_requirements=detail_requirements,
                    detail_preferred=detail_preferred,
                    jd_body=jd_body,
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    manifest = extract_requirement_manifest(batch.records[0].jd_markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == expected_manifest


def test_saramin_extraction_preserves_intro_content_with_semantic_boundaries(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616304"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616304",
                    detail_requirements="",
                    detail_preferred="",
                    jd_body=textwrap.dedent("""\
                        회사 소개
                        안정적인 SaaS 운영과 검색 API 제공 경험을 바탕으로 팀 협업과 서비스 안정성을 함께 높이는 포지션입니다.
                        자격요건
                        Python 백엔드 개발 경험
                        SQL 활용 능력
                        우대사항
                        테스트 코드 작성 경험
                        복리후생
                        원격 근무 가능
                    """),
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "포지션 소개") == (
        "회사 소개\n안정적인 SaaS 운영과 검색 API 제공 경험을 바탕으로 팀 협업과 서비스 안정성을 함께 높이는 포지션입니다.\n자격요건\nPython 백엔드 개발 경험\nSQL 활용 능력\n우대사항\n"
        "테스트 코드 작성 경험\n복리후생\n원격 근무 가능"
    )
    assert _section_body(markdown, "자격 요건") == "- Python 백엔드 개발 경험\n- SQL 활용 능력"
    assert _section_body(markdown, "우대사항") == "- 테스트 코드 작성 경험"

    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("Python 백엔드 개발 경험", RequirementKind.REQUIRED),
        ("SQL 활용 능력", RequirementKind.REQUIRED),
        ("테스트 코드 작성 경험", RequirementKind.PREFERRED),
    ]


def test_saramin_extraction_handles_missing_requirement_sources(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616305"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616305",
                    detail_requirements="",
                    detail_preferred="",
                    jd_body="주요업무\n백엔드 API 개발",
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "자격 요건") == "정보 없음"
    assert _section_body(markdown, "우대사항") == "정보 없음"

    manifest = extract_requirement_manifest(markdown)

    assert manifest.parents == ()
    assert manifest.ambiguous_qualifications is False


def test_saramin_extraction_flows_through_extract_with_manifest_boundaries(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=54616306"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url=_saramin_text_by_url(
                url,
                _saramin_detail_html(
                    "54616306",
                    detail_requirements="<ul><li>상세 자격 우선</li></ul>",
                    detail_preferred="",
                    jd_body=textwrap.dedent("""\
                        회사 소개
                        검색 플랫폼 운영
                        우대사항
                        본문 우대 보강
                        자격요건
                        본문 자격 대체 금지
                    """),
                ),
            )
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert "회사 소개\n검색 플랫폼 운영" in _section_body(markdown, "포지션 소개")

    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("상세 자격 우선", RequirementKind.REQUIRED),
        ("본문 우대 보강", RequirementKind.PREFERRED),
    ]


def test_groupby_extraction_preserves_startup_metadata(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://groupby.kr/positions/456"
    api_url = "https://api.groupby.kr/startup-positions/456"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            json_by_url={
                api_url: {
                    "status": 200,
                    "data": {
                        "name": "Backend Engineer",
                        "careerType": "경력",
                        "experienceRange": {"min": 4, "max": 8},
                        "task": "<p>서비스 개발</p>",
                        "startup": {
                            "name": "Group Co",
                            "briefIntro": "B2B 데이터 플랫폼",
                            "memberCount": 40,
                            "devCount": 12,
                            "fundingRound": "Series A",
                            "serviceAreas": ["SaaS", "Data"],
                        },
                    },
                }
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    for expected in (
        "B2B 데이터 플랫폼",
        "40",
        "12",
        "Series A",
        "SaaS",
        "Data",
        "서비스 개발",
        "4~8년",
    ):
        assert expected in markdown


def test_run_auto_keeps_only_failed_urls_for_resume_after_partial_batch() -> None:
    class PartialExtraction(FakeExtractionStage):
        def extract(self, urls, *, dry_run, screening_only):
            batch = super().extract(urls[1:], dry_run=dry_run, screening_only=screening_only)
            return replace(batch, failed_urls=(urls[0],), metadata={**batch.metadata, "failure_count": 1})

    resume_state = FakeResumeState()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=PartialExtraction(),
        screening_stage=FakeScreeningStage(),
        completion_stage=FakeCompletionStage(),
        resume_state=resume_state,
    )

    result = service.run("auto", ["--json"])

    assert result.returncode == 0
    assert resume_state.saved[-1] == ("https://wanted.example/1",)
    assert resume_state.cleared == 0


def test_run_auto_keeps_screening_failures_for_resume() -> None:
    class PartialScreening(FakeScreeningStage):
        def screen(self, extraction, *, dry_run, llm_timeout, local_llm_timeout=None):
            self.calls.append(
                (tuple(extraction.item_ids), dry_run, llm_timeout, local_llm_timeout)
            )
            return ScreeningBatch(
                item_ids=(extraction.item_ids[1],),
                metadata={
                    "failure_count": 1,
                    "failures": [{"job_key": extraction.item_ids[0], "error": "invalid"}],
                },
            )

    resume_state = FakeResumeState()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=FakeExtractionStage(),
        screening_stage=PartialScreening(),
        completion_stage=FakeCompletionStage(),
        resume_state=resume_state,
    )

    result = service.run("auto", ["--json"])

    assert result.returncode == 0
    assert resume_state.saved[-1] == ("https://wanted.example/1",)
    assert resume_state.cleared == 0


def test_run_auto_keeps_company_info_warned_urls_for_resume() -> None:
    class WarnedScreening(FakeScreeningStage):
        def screen(self, extraction, *, dry_run, llm_timeout, local_llm_timeout=None):
            self.calls.append(
                (tuple(extraction.item_ids), dry_run, llm_timeout, local_llm_timeout)
            )
            return ScreeningBatch(
                item_ids=extraction.item_ids,
                metadata={
                    "failure_count": 0,
                    "failures": [],
                    "company_info_warnings": {
                        extraction.item_ids[0]: "company info file missing",
                    },
                },
            )

    resume_state = FakeResumeState()
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=FakeExtractionStage(),
        screening_stage=WarnedScreening(),
        completion_stage=FakeCompletionStage(),
        resume_state=resume_state,
    )

    result = service.run("auto", ["--json"])

    assert result.returncode == 0
    assert resume_state.saved[-1] == ("https://wanted.example/1",)
    assert resume_state.cleared == 0


def test_screening_stage_prescreens_closed_and_recent_prior_application(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    closed = repository.create(
        JobRecord("wanted", "1", "Closed Co", "Backend"),
        jd_markdown="# JD\n\n채용 마감\n",
    )
    repository.create(
        JobRecord(
            "wanted",
            "2",
            "Prior Co",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at=datetime.now().isoformat(),
        ),
        jd_markdown="# Prior\n",
    )
    current = repository.create(
        JobRecord("remember", "3", "Prior Co", "Backend"),
        jd_markdown="# Current\n",
    )
    stage = JobsScreeningStage(workspace=workspace, repository=repository)

    result = stage.screen(
        ExtractionBatch(("closed", "prior"), ("wanted:1", "remember:3"), (closed, current), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ()
    assert result.metadata["prescreen_reasons"] == {"closed": 1, "prior_application": 1}
    assert repository.get(JobKey("wanted", "1")).record.posting_status is PostingStatus.CLOSED
    assert repository.get(JobKey("remember", "3")).record.application_status is ApplicationStatus.REJECTED
    assert repository.get(JobKey("remember", "3")).record.screening_verdict is ScreeningVerdict.NOT_RECOMMENDED


def test_screening_stage_logs_each_record_at_debug(tmp_path: Path, caplog) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    closed = repository.create(
        JobRecord("wanted", "1", "Closed Co", "Backend"),
        jd_markdown="# JD\n\n채용 마감\n",
    )
    caplog.set_level(logging.DEBUG, logger="careerkit.jobs.application.automation")

    JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("closed",), ("wanted:1",), (closed,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert "screening: wanted:1" in caplog.messages


def _screening_result(**overrides):
    """Stand-in matching ScreeningResult's shape, including the fields the stage aggregates."""
    fields = {
        "verdict": "지원 추천",
        "provider": "fake",
        "used_fallback": False,
        "verdict_capped": False,
        "downgraded": False,
        "evidence_violations": {},
        "provider_attempts": {},
        "context_warning": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_screening_stage_prescreens_title_and_domain_for_url_batches(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    (tmp_path / "private/jd/config/search_config.yaml").write_text(
        "search:\n  role: backend\nquick_filters:\n  title_exclude:\n    - Product Manager\n",
        encoding="utf-8",
    )
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _write_valid_company_info(tmp_path, "mixed-co", "Mixed Co")
    title_excluded = repository.create(
        JobRecord("wanted", "10", "Product Co", "Product Manager"),
        jd_markdown="# Product Manager\n",
    )
    domain_excluded = repository.create(
        JobRecord("wanted", "11", "Frontend Co", "Frontend Engineer"),
        jd_markdown="# Frontend Engineer\n",
    )
    counter_indicator = repository.create(
        JobRecord("wanted", "12", "Mixed Co", "Backend / Frontend Engineer"),
        jd_markdown="# Backend / Frontend Engineer\n",
    )
    screened: list[str] = []

    def fake_run_screening(**kwargs):
        screened.append(kwargs["jd"].record.job_id)
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("title", "domain", "counter"),
            ("wanted:10", "wanted:11", "wanted:12"),
            (title_excluded, domain_excluded, counter_indicator),
            {},
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:12",)
    assert result.metadata["prescreen_reasons"] == {"domain_frontend": 1, "title_exclude": 1}
    assert screened == ["12"]
    for job_id in ("10", "11"):
        record = repository.get(JobKey("wanted", job_id)).record
        assert record.application_status is ApplicationStatus.PENDING
        assert record.screening_verdict is ScreeningVerdict.NOT_RECOMMENDED


def test_screening_only_bypasses_prescreen_filters(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _write_valid_company_info(tmp_path, "product-co", "Product Co")
    record = repository.create(
        JobRecord("wanted", "20", "Product Co", "Product Manager"),
        jd_markdown="# Product Manager\n",
    )
    screened = []

    def fake_run_screening(**kwargs):
        screened.append(kwargs["jd"].record.job_id)
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)
    result = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Product Manager"]},
    ).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:20",),
            (record,),
            {"mode": "screening_only"},
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:20",)
    assert screened == ["20"]


def test_screening_stage_passes_matching_company_info_file(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    company_file = _write_valid_company_info(tmp_path, "acme", "Acme Inc.")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Acme Inc.", "Backend"),
        jd_markdown="# JD\n",
    )
    captured = {}

    def fake_run_screening(**kwargs):
        captured["company_file"] = kwargs["company_file"]
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:1",), (record,), {}),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:1",)
    assert captured["company_file"] == company_file


def test_screening_stage_continues_after_one_invalid_llm_result(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "acme", "Acme")
    _write_valid_company_info(tmp_path, "beta", "Beta")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    first = repository.create(JobRecord("wanted", "1", "Acme", "Backend"), jd_markdown="# One\n")
    second = repository.create(JobRecord("wanted", "2", "Beta", "Backend"), jd_markdown="# Two\n")

    def fake_run_screening(**kwargs):
        if kwargs["jd"].record.job_id == "1":
            raise ValueError("invalid screening structure after retry")
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("one", "two"), ("wanted:1", "wanted:2"), (first, second), {}),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:2",)
    assert result.metadata["failure_count"] == 1
    assert result.metadata["failures"][0]["job_key"] == "wanted:1"


def test_screening_stage_proceeds_when_company_info_is_missing(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Missing Co", "Backend"),
        jd_markdown="# JD\n",
    )
    captured = {}

    def fake_run_screening(**kwargs):
        captured["company_file"] = kwargs["company_file"]
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:1",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:1",)
    assert result.metadata["failures"] == []
    assert result.metadata["company_info_warnings"] == {
        "wanted:1": _COMPANY_INFO_MISSING,
    }
    assert captured["company_file"] is None


def test_screening_stage_warns_on_incomplete_company_info(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    bad_file = tmp_path / "private/company_info" / "broken-co.md"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("# Broken Co\n\nnot valid markdown table", encoding="utf-8")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Broken Co", "Backend"),
        jd_markdown="# JD\n",
    )
    captured = {}

    def fake_run_screening(**kwargs):
        captured["company_file"] = kwargs["company_file"]
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:1",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:1",)
    assert result.metadata["company_info_warnings"] == {
        "wanted:1": _COMPANY_INFO_INCOMPLETE,
    }
    assert captured["company_file"] == bad_file


def test_screening_stage_blocks_on_validation_errors(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "bad-co", "Bad Co")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Bad Co", "Backend"),
        jd_markdown="# JD\n",
    )

    def fake_validate(self, *, file_name=None, fix=False, now=None):
        return CompanyValidationSummary(
            processed_files=0,
            error_files=1,
            critical_risk_companies=0,
            high_risk_companies=0,
            incomplete_companies=0,
            results=(),
            errors=("bad-co.md: corrupt file",),
            fixed_files=(),
        )

    monkeypatch.setattr(CompanyInfoService, "validate", fake_validate)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:1",), (record,), {}),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ()
    assert result.metadata["failure_count"] == 1
    assert result.metadata["failures"][0]["job_key"] == "wanted:1"
    assert result.metadata["company_info_warnings"] == {}


def test_screening_stage_warns_each_record_for_shared_missing_company(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    rec_a = repository.create(
        JobRecord("wanted", "1", "Ghost Co", "Backend Developer"),
        jd_markdown="# JD A\n",
    )
    rec_b = repository.create(
        JobRecord("wanted", "2", "Ghost Co", "Backend Engineer"),
        jd_markdown="# JD B\n",
    )

    def fake_run_screening(**kwargs):
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("a", "b"), ("wanted:1", "wanted:2"), (rec_a, rec_b), {}),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:1", "wanted:2")
    assert result.metadata["company_info_warnings"] == {
        "wanted:1": _COMPANY_INFO_MISSING,
        "wanted:2": _COMPANY_INFO_MISSING,
    }


def test_auto_result_service_persists_success_and_failure_payloads(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    result_store = JobsAutoResultService(workspace=workspace)
    success = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=FakeExtractionStage(),
        screening_stage=FakeScreeningStage(),
        completion_stage=FakeCompletionStage(),
        result_store=result_store,
    ).run("auto", ["--json"])

    assert success.returncode == 0
    success_payload = json.loads(success.stdout)
    success_path = tmp_path / success_payload["result_path"]
    assert success_path.name.startswith("auto_")
    assert json.loads(success_path.read_text(encoding="utf-8"))["returncode"] == 0

    failure = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        result_store=result_store,
    ).run("auto", ["--json"])

    assert failure.returncode == 2
    result_files = sorted((tmp_path / "private/jd/runtime/auto/results").glob("auto_*.json"))
    assert len(result_files) == 2
    failure_payload = json.loads(result_files[-1].read_text(encoding="utf-8"))
    assert failure_payload["returncode"] == 2
    assert "extraction stage" in failure_payload["error"]


def test_run_auto_real_services_extract_screen_classify_and_clear_resume_state(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "goldenco", "GoldenCo")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    maintenance = JobsMaintenanceService(workspace=workspace)
    pipeline = JobsPipelineService(
        workspace_root=workspace.root,
        repository=repository,
        runtime_dir=maintenance.runtime_dir,
    )
    resume_state = JobsResumeStateService(workspace=workspace)
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "position": "Senior Backend Engineer",
                    "company": {"company_name": "GoldenCo"},
                    "career": {"annual_from": 3, "annual_to": 7},
                    "address": {"full_location": "Seoul"},
                    "intro": "서비스 소개",
                    "main_tasks": "• 백엔드 시스템 개발",
                    "requirements": "• Python\n• API",
                    "preferred_points": "• 검색 경험",
                    "benefits": "점심 제공",
                }
            }
        }
    }
    http_client = FakeHttpClient(
        text_by_url={
            "https://www.wanted.co.kr/wd/123456": f'<html><script id="__NEXT_DATA__">{json.dumps(payload, ensure_ascii=False)}</script></html>',
        }
    )
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=JobsExtractionStage(repository=repository, http_client=http_client),
        screening_stage=JobsScreeningStage(
            workspace=workspace,
            repository=repository,
            llm_provider=FakeProvider(GOLDEN_ASSESSMENT_JSON, provider_name="fake"),
            candidate_context="[source: synthetic/profile.md] fixed candidate context",
        ),
        completion_stage=JobsCompletionStage(
            pipeline=pipeline,
            maintenance=maintenance,
        ),
        resume_state=resume_state,
    )
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://www.wanted.co.kr/wd/123456\n", encoding="utf-8")

    result = service.run("auto", ["--from-urls", str(url_file), "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "complete"
    assert payload["extraction"]["item_ids"] == ["wanted:123456"]
    assert payload["extraction"]["items"] == [
        {"job_key": "wanted:123456", "company": "GoldenCo", "position": "Senior Backend Engineer"}
    ]
    assert payload["screening"]["verdict_counts"] == {"지원 추천": 1}
    assert payload["completion"]["summary_rebuilt"] is True
    stored = repository.get(JobKey("wanted", "123456"))
    assert stored.record.company == "GoldenCo"
    assert stored.record.position == "Senior Backend Engineer"
    assert stored.record.screening_verdict is ScreeningVerdict.RECOMMENDED
    assert stored.screening_markdown is not None
    assert "### 최종 판정: 지원 추천" in stored.screening_markdown
    assert "Python" in stored.screening_markdown
    summary_path = tmp_path / "private/jd/derived/screening-summary.md"
    assert summary_path.exists()
    assert "wanted:123456" in summary_path.read_text(encoding="utf-8")
    assert resume_state.load_pending_urls() == ()


def test_run_auto_and_queue_rescreen_share_the_same_structured_manifest_contract(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "goldenco", "GoldenCo")
    (tmp_path / "private/profile").mkdir(parents=True)
    (tmp_path / "private/profile/summary-job.md").write_text("summary", encoding="utf-8")
    (tmp_path / "private/profile/skills-job.md").write_text(
        "Python\nAPI\n백엔드 시스템 개발\n검색 경험",
        encoding="utf-8",
    )
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    maintenance = JobsMaintenanceService(workspace=workspace)
    pipeline = JobsPipelineService(
        workspace_root=workspace.root,
        repository=repository,
        runtime_dir=maintenance.runtime_dir,
    )
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "position": "Senior Backend Engineer",
                    "company": {"company_name": "GoldenCo"},
                    "career": {"annual_from": 3, "annual_to": 7},
                    "address": {"full_location": "Seoul"},
                    "intro": "서비스 소개",
                    "main_tasks": "• 백엔드 시스템 개발",
                    "requirements": "• Python\n• API",
                    "preferred_points": "• 검색 경험",
                    "benefits": "점심 제공",
                }
            }
        }
    }
    http_client = FakeHttpClient(
        text_by_url={
            "https://www.wanted.co.kr/wd/123456": (
                f'<html><script id="__NEXT_DATA__">'
                f"{json.dumps(payload, ensure_ascii=False)}</script></html>"
            ),
        },
    )
    candidate_context = cli.load_candidate_context(workspace)
    assessment = json.loads(GOLDEN_ASSESSMENT_JSON)
    for match in assessment["matches"]:
        match["evidence"] = (
            f"[source: private/profile/skills-job.md] {match['id']} 근거"
        )
    assessment_json = json.dumps(assessment, ensure_ascii=False)
    auto_provider = CapturingFakeProvider(assessment_json, provider_name="local")
    service = AutomationService(
        search_port=FakeSearchPort(_search_result()),
        extraction_stage=JobsExtractionStage(repository=repository, http_client=http_client),
        screening_stage=JobsScreeningStage(
            workspace=workspace,
            repository=repository,
            llm_provider=auto_provider,
            candidate_context=candidate_context,
        ),
        completion_stage=JobsCompletionStage(
            pipeline=pipeline,
            maintenance=maintenance,
        ),
    )
    url_file = tmp_path / "urls.txt"
    url_file.write_text("https://www.wanted.co.kr/wd/123456\n", encoding="utf-8")

    result = service.run("auto", ["--from-urls", str(url_file), "--json"])

    assert result.returncode == 0
    auto_payload = json.loads(result.stdout)
    assert auto_payload["screening"]["fallback_count"] == 0
    assert auto_payload["screening"]["providers"] == {"local": 1}
    assert auto_payload["screening"]["failure_count"] == 0
    assert auto_payload["screening"]["verdict_counts"] == {"지원 보류": 1}
    assert len(auto_provider.prompts) == 1

    rescreen_provider = CapturingFakeProvider(assessment_json, provider_name="codex")
    bundle = cli.ServiceBundle(
        maintenance=maintenance,
        pipeline=pipeline,
        automation=service,
    )
    monkeypatch.setattr(cli, "resolve_workspace", lambda explicit=None: workspace)
    monkeypatch.setattr(cli, "_build_services", lambda resolved: bundle)
    original_run_screening = cli.run_screening
    monkeypatch.setattr(
        cli,
        "run_screening",
        lambda **kwargs: original_run_screening(llm_provider=rescreen_provider, **kwargs),
    )

    assert cli.main(["queue", "rescreen", "wanted:123456", "--dry-run", "--json"]) == 0
    rescreen_payload = json.loads(capsys.readouterr().out)
    assert rescreen_payload["items"][0]["outcome"] == "success"
    assert rescreen_payload["items"][0]["verdict"] == "지원 추천"
    assert len(rescreen_provider.prompts) == 1

    auto_manifest = _manifest_json_from_prompt(auto_provider.prompts[0])
    rescreen_manifest = _manifest_json_from_prompt(rescreen_provider.prompts[0])

    assert auto_manifest.encode("utf-8") == rescreen_manifest.encode("utf-8")
    assert "JSON 객체 하나만 허용" in auto_provider.prompts[0]
    assert "JSON 객체 하나만 허용" in rescreen_provider.prompts[0]


def test_completion_stage_dry_run_skips_repository_classification(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    maintenance = JobsMaintenanceService(workspace=workspace)
    pipeline = JobsPipelineService(
        workspace_root=workspace.root,
        repository=repository,
        runtime_dir=maintenance.runtime_dir,
    )
    stage = JobsCompletionStage(pipeline=pipeline, maintenance=maintenance)
    preview = StoredJobRecord(
        record=JobRecord(
            platform="wanted",
            job_id="1",
            company="PreviewCo",
            position="Backend Engineer",
        ),
        jd_markdown="# Preview\n",
    )

    result = stage.complete(
        ExtractionBatch(
            urls=("https://www.wanted.co.kr/wd/1",),
            item_ids=("wanted:1",),
            records=(preview,),
            metadata={},
        ),
        ScreeningBatch(item_ids=("wanted:1",), metadata={}),
        dry_run=True,
        no_classify=False,
    )

    assert result.metadata["results"] == [
        {
            "job_key": "wanted:1",
            "outcome": "skipped",
            "message": "dry-run preview",
        }
    ]
    assert result.metadata["summary_rebuilt"] is False


def _screened_batch(tmp_path: Path, monkeypatch, results: list) -> dict:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "acme", "Acme Inc.")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    records = []
    for index in range(len(results)):
        records.append(
            repository.create(
                JobRecord("wanted", str(900 + index), "Acme Inc.", "Backend"),
                jd_markdown="# JD\n",
            )
        )
    pending = list(results)

    def fake_run_screening(**kwargs):
        return pending.pop(0)

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening", fake_run_screening
    )
    batch = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",) * len(records),
            tuple(f"wanted:{900 + i}" for i in range(len(records))),
            tuple(records),
            {},
        ),
        dry_run=True,
        llm_timeout=1,
    )
    return batch.metadata


def test_screening_metadata_counts_caps_and_downgrades(tmp_path: Path, monkeypatch) -> None:
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(verdict="지원 보류", provider="ollama", verdict_capped=True),
            _screening_result(verdict="지원 보류", provider="ollama", verdict_capped=True),
            _screening_result(verdict="지원 보류", provider="codex", downgraded=True),
        ],
    )

    assert metadata["capped"] == 2
    assert metadata["downgraded"] == 1
    assert metadata["providers"] == {"codex": 1, "ollama": 2}


def test_screening_metadata_sums_evidence_violations(tmp_path: Path, monkeypatch) -> None:
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(evidence_violations={"unevidenced_keyword": 2}),
            _screening_result(
                evidence_violations={"unevidenced_keyword": 1, "missing_source_path": 3}
            ),
        ],
    )

    assert metadata["evidence_violations"] == {
        "missing_source_path": 3,
        "unevidenced_keyword": 3,
    }


def test_screening_metadata_exposes_provider_attempts_and_context_warnings(
    tmp_path: Path, monkeypatch
) -> None:
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                provider="ollama",
                provider_attempts={"claude": ("command not found",), "ollama": ("ok",)},
                context_warning="ollama: prompt 30000 tokens exceeds 90% of num_ctx 32768",
            )
        ],
    )

    assert metadata["provider_attempts"] == {
        "claude": {"command not found": 1},
        "ollama": {"ok": 1},
    }
    assert metadata["context_warnings"] == 1


def test_screening_metadata_keeps_every_provider_attempt_across_a_batch(
    tmp_path: Path, monkeypatch
) -> None:
    """Last-write-wins would report only claude:ok, hiding the timeout this
    telemetry exists to surface."""
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                provider="ollama",
                provider_attempts={"claude": ("timed out after 120s",), "ollama": ("ok",)},
            ),
            _screening_result(
                provider="claude",
                provider_attempts={"claude": ("ok",)},
            ),
        ],
    )

    assert metadata["provider_attempts"] == {
        "claude": {"ok": 1, "timed out after 120s": 1},
        "ollama": {"ok": 1},
    }


def test_screening_metadata_keys_exist_for_an_empty_batch(tmp_path: Path, monkeypatch) -> None:
    metadata = _screened_batch(tmp_path, monkeypatch, [])

    assert metadata["capped"] == 0
    assert metadata["downgraded"] == 0
    assert metadata["context_warnings"] == 0
    assert metadata["evidence_violations"] == {}
    assert metadata["provider_attempts"] == {}


def test_screening_metadata_preserves_existing_keys(tmp_path: Path, monkeypatch) -> None:
    metadata = _screened_batch(tmp_path, monkeypatch, [_screening_result()])

    for key in (
        "item_ids",
        "verdict_counts",
        "providers",
        "fallback_count",
        "failure_count",
        "failures",
        "prescreened_count",
        "prescreen_reasons",
    ):
        assert key in metadata


def test_screening_metadata_keeps_the_context_warning_message(
    tmp_path: Path, monkeypatch
) -> None:
    warning = "ollama: prompt 30000 tokens exceeds 90% of num_ctx 32768"
    metadata = _screened_batch(
        tmp_path, monkeypatch, [_screening_result(context_warning=warning)]
    )

    assert metadata["context_warnings"] == 1
    assert metadata["context_warning_messages"] == [warning]


def test_screening_metadata_deduplicates_identical_context_warnings(
    tmp_path: Path, monkeypatch
) -> None:
    warning = "ollama: prompt 30000 tokens exceeds 90% of num_ctx 32768"
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [_screening_result(context_warning=warning), _screening_result(context_warning=warning)],
    )

    assert metadata["context_warnings"] == 2
    assert metadata["context_warning_messages"] == [warning]
