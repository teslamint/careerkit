from __future__ import annotations

import base64
import json
import logging
import os
import stat
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from careerkit.jobs import cli
from careerkit.jobs.adapters.platforms.saramin import SaraminAdapter
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
    _COMPANY_INFO_FAILURE_CODE,
    _COMPANY_INFO_MISSING,
    _COMPANY_INFO_INCOMPLETE,
    _enrichment_context_with_fetched_facts,
    _render_json,
)
from careerkit.jobs.application.company_enrichment import (
    CompanyEnrichmentContext,
    CompanyInfoEnrichmentResult,
)
from careerkit.jobs.application.company_info import CompanyInfoService, CompanyValidationSummary
from careerkit.jobs.application.config import load_runtime_config
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.requirement_manifest import RequirementKind, extract_requirement_manifest, without_main_duty
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.application.search import SearchCandidate, SearchResult, SearchService, SearchState
from careerkit.jobs.domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)
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


class SequenceJsonClient:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def request_json(self, url: str, **_: Any) -> object:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _saramin_search_card_html(job_id: int) -> str:
    return (
        f'<div id="list_{job_id}" class="recruit_container list_link recruit" data-rec_idx={job_id}>'
        f'<p class="tit">Backend Engineer {job_id}</p>'
        '<div class="meta"><span>서울</span><span>경력5년↑</span></div>'
        '<div class="corp"><span class="corp_name">테스트회사</span></div>'
        '</div>'
    )


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


def _search_result_with_pagination_diagnostic() -> SearchResult:
    postings = tuple(
        SearchCandidate(
            "saramin",
            str(index),
            f"saramin:{index}",
            f"Backend {index}",
            "Acme",
            "5년",
            f"https://saramin.example/{index}",
        )
        for index in range(1, 4)
    )
    return SearchResult(
        postings=postings,
        updated_seen_job_keys={item.seen_key for item in postings},
        total_found=3,
        diagnostics=("saramin search incomplete after 7 pages: safety_page_limit",),
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


def _section_body(markdown: str, heading: str) -> str:
    marker = f"## {heading}\n\n"
    start = markdown.index(marker) + len(marker)
    end = markdown.find("\n## ", start)
    if end == -1:
        end = len(markdown)
    return markdown[start:end].strip()




def _saramin_detail_html(*, job_id: str, body_html: str, detail_pairs: tuple[tuple[str, str], ...]) -> str:
    encoded_body = base64.b64encode(body_html.encode(encoding="utf-8")).decode(encoding="utf-8")
    details = "".join(
        f'<dt class="tit">{label}</dt><dd class="desc">{value}</dd>'
        for label, value in detail_pairs
    )
    return (
        f'<html><head><title>[테스트회사] Backend Engineer (D-7) - 사람인</title></head>'
        f'<body><span class="corp_name">테스트회사</span><dl>{details}</dl>'
        f"<script>var detailContents_{job_id} = {{contents: '{encoded_body}', mobile_contents_yn: ''}};</script>"
        f'</body></html>'
    )

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


def test_run_auto_search_only_preserves_pagination_diagnostic_and_final_cap(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    search_port = FakeSearchPort(_search_result_with_pagination_diagnostic())
    service = AutomationService(
        search_port=search_port,
        result_store=JobsAutoResultService(workspace=workspace),
    )

    result = service.run("auto", ["--search-only", "--max-urls", "1", "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["url_count"] == 1
    assert payload["counts"] == {
        "total_found": 3,
        "returned": 1,
        "filtered_out": 0,
        "duplicates": 0,
    }
    assert payload["diagnostics"] == [
        "saramin search incomplete after 7 pages: safety_page_limit",
    ]
    request_path = tmp_path / payload["request_path"]
    assert request_path.read_text(encoding="utf-8").splitlines() == [
        "https://saramin.example/1",
    ]
    assert search_port.max_urls == [1]
    assert search_port.persisted == [{"saramin:1"}]


def test_run_auto_search_only_composes_saramin_pages_through_search_and_json(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    http = SequenceJsonClient(
        [
            {"count": 6, "innerHTML": _saramin_search_card_html(job_id)}
            for job_id in range(1, 7)
        ]
        + [{"count": 6, "innerHTML": ""}]
    )
    config = replace(
        load_runtime_config(
            {
                "search": {"role": "backend"},
                "platforms": {"saramin": {"enabled": True}},
                "search_queries": ["Backend"],
                "execution": {"max_urls_per_run": 10},
                "quick_filters": {"title_include": ["Backend"], "title_exclude": [], "title_prefer": []},
                "filters": {"min_experience_upper": 9, "max_experience": 10},
                "semantic_filter": {"enabled": False},
            }
        ),
        http_client=http,
        rate_limits={"saramin": 0.0},
    )
    search_result = SearchService(
        adapters={"saramin": SaraminAdapter()}, semantic_filter=None
    ).run(config, SearchState(seen_job_keys=set()))
    search_port = FakeSearchPort(search_result)

    result = AutomationService(
        search_port=search_port,
        result_store=JobsAutoResultService(workspace=workspace),
    ).run("auto", ["--search-only", "--max-urls", "2", "--json"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["total_found"] == 6
    assert payload["counts"]["returned"] == 2
    assert len(http.urls) == 7
    request_path = tmp_path / payload["request_path"]
    assert len(request_path.read_text(encoding="utf-8").splitlines()) == 2


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


def test_jobs_extraction_stage_screening_only_reuses_complete_record_jd(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    complete_jd = "# Backend Engineer\n\n## 자격 요건\n\n- Java 3년 이상\n- Spring Boot 경험\n"
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="123456",
            company="Acme",
            position="Backend",
            source_url="https://www.wanted.co.kr/wd/123456",
        ),
        jd_markdown=complete_jd,
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
    assert batch.records[0].jd_markdown == complete_jd
    assert batch.records[0].record.company == "Acme"


def test_jobs_extraction_stage_logs_each_url_at_debug(tmp_path: Path, caplog) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    complete_jd = "# Backend Engineer\n\n## 자격 요건\n\n- Java 3년 이상\n"
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="123456",
            company="Acme",
            position="Backend",
            source_url="https://www.wanted.co.kr/wd/123456",
        ),
        jd_markdown=complete_jd,
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
    assert batch.company_contexts["wanted:123456"] == CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:123456",
        company_name="GoldenCo",
        company_id=None,
        source_url=url,
        facts={},
        fact_sources={},
    )


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
    assert batch.company_contexts["remember:123"] == CompanyEnrichmentContext(
        platform="remember",
        item_id="remember:123",
        company_name="Remember Co",
        company_id=None,
        source_url=url,
        facts={},
        fact_sources={},
    )


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
    assert batch.company_contexts["groupby:456"] == CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:456",
        company_name="Group Co",
        company_id="456",
        source_url=url,
        facts={
            "industry": "SaaS, Data",
            "employee_current": 40,
            "investment_round": "Series A",
            "is_startup": True,
        },
        fact_sources={
            "industry": (url,),
            "employee_current": (url,),
            "investment_round": (url,),
            "is_startup": (url,),
        },
    )


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
    prior_timestamp = datetime.now().isoformat()
    repository.create(
        JobRecord(
            "wanted",
            "2",
            "Prior Co",
            "Backend",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at=prior_timestamp,
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at=prior_timestamp,
                    note=None,
                ),
            ),
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
    current_record = repository.get(JobKey("remember", "3")).record
    assert current_record.application_status is ApplicationStatus.REJECTED
    assert current_record.screening_verdict is None
    assert current_record.prescreen_reason == "prior_application"
    assert len(current_record.application_history) == 1
    assert current_record.application_history[0].status is ApplicationStatus.REJECTED
    assert current_record.application_history[0].note is None


def test_screening_stage_enriches_before_prescreen_branching(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    closed = repository.create(
        JobRecord("wanted", "1", "Closed Co", "Backend"),
        jd_markdown="# JD\n\n채용 마감\n",
    )
    prior = repository.create(
        JobRecord("remember", "3", "Prior Co", "Backend"),
        jd_markdown="# Current\n",
    )
    seen: list[str] = []

    def fake_enrich(self, context, *, dry_run=False, timeout=1.0):
        del self, dry_run, timeout
        seen.append(context.item_id)
        return CompanyInfoEnrichmentResult(
            status="warning",
            attempted=True,
            persisted=False,
            completeness=None,
            warning_code="missing",
            file_path=None,
        )

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.CompanyEnrichmentService.enrich",
        fake_enrich,
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("closed", "prior"),
            ("wanted:1", "remember:3"),
            (closed, prior),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Closed Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={},
                    fact_sources={},
                ),
                "remember:3": CompanyEnrichmentContext(
                    platform="remember",
                    item_id="remember:3",
                    company_name="Prior Co",
                    company_id="3",
                    source_url="https://career.rememberapp.co.kr/job/posting/3",
                    facts={"industry": "IT"},
                    fact_sources={"industry": ("https://career.rememberapp.co.kr/job/company/3",)},
                ),
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert seen == ["wanted:1", "remember:3"]
    assert result.item_ids == ()
    assert result.metadata["company_info_results"] == {
        "remember:3": {
            "attempted": True,
            "completeness": None,
            "persisted": False,
            "status": "warning",
            "warning_code": "missing",
        },
        "wanted:1": {
            "attempted": True,
            "completeness": None,
            "persisted": False,
            "status": "warning",
            "warning_code": "missing",
        },
    }
    assert result.metadata["company_info_warnings"] == {
        "remember:3": _COMPANY_INFO_MISSING,
        "wanted:1": _COMPANY_INFO_MISSING,
    }


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
        "fallback_reason": None,
        "verdict_capped": False,
        "downgraded": False,
        "evidence_violations": {},
        "provider_attempts": {},
        "context_warning": None,
        "published": False,
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
    for job_id, expected_reason in {"10": "title_exclude", "11": "domain_frontend"}.items():
        record = repository.get(JobKey("wanted", job_id)).record
        assert record.application_status is ApplicationStatus.PENDING
        assert record.screening_verdict is None
        assert record.prescreen_reason == expected_reason


def test_pre_screen_writes_reason_without_verdict(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    excluded = repository.create(
        JobRecord("wanted", "30", "Synthetic Co", "Synthetic Excluded Role"),
        jd_markdown="# Synthetic Excluded Role\n",
    )

    result = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Synthetic Excluded Role"]},
    ).screen(
        ExtractionBatch(("url",), ("wanted:30",), (excluded,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ()
    assert result.metadata["prescreen_reasons"] == {"title_exclude": 1}
    stored = repository.get(JobKey("wanted", "30")).record
    assert stored.screening_verdict is None
    assert stored.prescreen_reason == "title_exclude"


def _jd_with_requirements(title: str, *, backend: bool) -> str:
    requirement = (
        "- Python 기반 API 서버 개발 경험 3년 이상"
        if backend
        else "- React 기반 웹 UI 개발 경험 3년 이상"
    )
    return f"# {title}\n\n## 자격요건\n\n{requirement}\n- 관계형 데이터베이스 스키마 설계 경험\n"


def test_backend_confirmed_title_is_not_pre_screened(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _write_valid_company_info(tmp_path, "synthetic-co", "Synthetic Co")
    record = repository.create(
        JobRecord("wanted", "34", "Synthetic Co", "Synthetic Excluded Role"),
        jd_markdown=_jd_with_requirements("Synthetic Excluded Role", backend=True),
    )
    screened: list[str] = []

    def fake_run_screening(**kwargs):
        screened.append(kwargs["jd"].record.job_id)
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Synthetic Excluded Role"]},
    ).screen(
        ExtractionBatch(("url",), ("wanted:34",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:34",)
    assert result.metadata["prescreen_reasons"] == {}
    assert screened == ["34"]
    assert repository.get(JobKey("wanted", "34")).record.prescreen_reason is None


def test_non_backend_requirements_keep_the_reason(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "35", "Synthetic Co", "Synthetic Excluded Role"),
        jd_markdown=_jd_with_requirements("Synthetic Excluded Role", backend=False),
    )

    result = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Synthetic Excluded Role"]},
    ).screen(
        ExtractionBatch(("url",), ("wanted:35",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ()
    assert result.metadata["prescreen_reasons"] == {"title_exclude": 1}
    assert repository.get(JobKey("wanted", "35")).record.prescreen_reason == "title_exclude"


def test_backend_confirmed_domain_title_is_not_pre_screened(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _write_valid_company_info(tmp_path, "synthetic-co", "Synthetic Co")
    record = repository.create(
        JobRecord("wanted", "36", "Synthetic Co", "Frontend Engineer"),
        jd_markdown=_jd_with_requirements("Frontend Engineer", backend=True),
    )
    screened: list[str] = []

    def fake_run_screening(**kwargs):
        screened.append(kwargs["jd"].record.job_id)
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:36",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:36",)
    assert result.metadata["prescreen_reasons"] == {}
    assert screened == ["36"]
    assert repository.get(JobKey("wanted", "36")).record.prescreen_reason is None


def test_backend_requirements_do_not_cancel_a_closed_posting(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "37", "Synthetic Co", "Synthetic Backend Role"),
        jd_markdown=_jd_with_requirements("Synthetic Backend Role", backend=True) + "\n채용 마감\n",
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:37",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.metadata["prescreen_reasons"] == {"closed": 1}
    stored = repository.get(JobKey("wanted", "37")).record
    assert stored.posting_status is PostingStatus.CLOSED
    assert stored.prescreen_reason == "closed"


def test_backend_requirements_do_not_cancel_a_prior_application(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    prior_timestamp = datetime.now().isoformat()
    repository.create(
        JobRecord(
            "wanted",
            "38",
            "Synthetic Co",
            "Synthetic Backend Role",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at=prior_timestamp,
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at=prior_timestamp,
                    note=None,
                ),
            ),
        ),
        jd_markdown="# Synthetic Backend Role\n",
    )
    record = repository.create(
        JobRecord("wanted", "39", "Synthetic Co", "Synthetic Backend Role"),
        jd_markdown=_jd_with_requirements("Synthetic Backend Role", backend=True),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:39",), (record,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.metadata["prescreen_reasons"] == {"prior_application": 1}
    assert repository.get(JobKey("wanted", "39")).record.prescreen_reason == "prior_application"


def test_closed_posting_still_marks_posting_status(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    closed = repository.create(
        JobRecord("wanted", "31", "Synthetic Co", "Synthetic Backend Role"),
        jd_markdown="# JD\n\n채용 마감\n",
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(("url",), ("wanted:31",), (closed,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.metadata["prescreen_reasons"] == {"closed": 1}
    stored = repository.get(JobKey("wanted", "31")).record
    assert stored.posting_status is PostingStatus.CLOSED
    assert stored.screening_verdict is None
    assert stored.prescreen_reason == "closed"


def test_pre_screen_write_failure_propagates_without_verdict_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    class _PrescreenWriteError(Exception):
        """Distinct from RuntimeError/ValueError so the run_screening guard cannot swallow it."""

    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    excluded = repository.create(
        JobRecord("wanted", "32", "Synthetic Co", "Synthetic Excluded Role"),
        jd_markdown="# Synthetic Excluded Role\n",
    )
    verdict_calls: list[tuple] = []

    def raise_on_prescreen(key, reason):
        del key, reason
        raise _PrescreenWriteError("write failed")

    monkeypatch.setattr(repository, "update_prescreen", raise_on_prescreen)
    monkeypatch.setattr(
        repository,
        "update_verdict",
        lambda *args, **kwargs: verdict_calls.append((args, kwargs)),
    )
    stage = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Synthetic Excluded Role"]},
    )

    with pytest.raises(_PrescreenWriteError):
        stage.screen(
            ExtractionBatch(("url",), ("wanted:32",), (excluded,), {}),
            dry_run=False,
            llm_timeout=1,
        )

    assert verdict_calls == []


def test_pre_screened_record_is_absent_from_verdict_counts(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    excluded = repository.create(
        JobRecord("wanted", "33", "Synthetic Co", "Synthetic Excluded Role"),
        jd_markdown="# Synthetic Excluded Role\n",
    )

    JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        quick_filters={"title_exclude": ["Synthetic Excluded Role"]},
    ).screen(
        ExtractionBatch(("url",), ("wanted:33",), (excluded,), {}),
        dry_run=False,
        llm_timeout=1,
    )

    counts: Counter[ScreeningVerdict | None] = Counter(
        stored.record.screening_verdict for stored in repository.list()
    )
    assert counts[None] == 1
    assert counts.get(ScreeningVerdict.NOT_RECOMMENDED, 0) == 0


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


def test_screening_stage_enriches_missing_company_info_before_screening(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Ready Co", "Backend"),
        jd_markdown="# JD\n",
    )
    captured = {}

    def fake_run_screening(**kwargs):
        captured["company_file"] = kwargs["company_file"]
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:1",),
            (record,),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Ready Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={
                        "founded_year": 2020,
                        "employee_current": 40,
                    },
                    fact_sources={
                        "founded_year": ("https://www.wanted.co.kr/wd/1",),
                        "employee_current": ("https://www.wanted.co.kr/wd/1",),
                    },
                )
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("wanted:1",)
    assert captured["company_file"] == tmp_path / "private/company_info/ready-co.md"
    assert result.metadata["company_info_results"] == {
        "wanted:1": {
            "attempted": True,
            "completeness": 100.0,
            "persisted": True,
            "status": "ready",
            "warning_code": None,
        }
    }
    assert result.metadata["company_info_warnings"] == {}


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
    assert result.metadata["company_info_results"] == {
        "wanted:1": {
            "attempted": False,
            "completeness": None,
            "persisted": False,
            "status": "warning",
            "warning_code": "missing",
        }
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
    assert result.metadata["company_info_results"] == {
        "wanted:1": {
            "attempted": False,
            "completeness": 0.0,
            "persisted": False,
            "status": "warning",
            "warning_code": "below_threshold",
        }
    }
    assert captured["company_file"] == bad_file


def test_screening_stage_rescreen_fetch_failure_keeps_existing_record_and_warning_path(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _complete_jd = "# Backend\n\n## 자격 요건\n\n- Java 3년 이상\n"
    repository.create(
        JobRecord("wanted", "123456", "GoldenCo", "Backend"),
        jd_markdown=_complete_jd,
    )
    url = "https://www.wanted.co.kr/wd/123456"
    stage = JobsExtractionStage(repository=repository, http_client=FakeHttpClient())

    batch = stage.extract([url], dry_run=True, screening_only=True)

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )
    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        batch,
        dry_run=True,
        llm_timeout=1,
    )

    assert batch.company_contexts == {}
    assert "자격 요건" in batch.records[0].jd_markdown
    assert "자격 요건" in repository.get(JobKey("wanted", "123456")).jd_markdown
    assert result.item_ids == ("wanted:123456",)
    assert result.metadata["company_info_warnings"] == {
        "wanted:123456": _COMPANY_INFO_MISSING,
    }
    assert result.metadata["company_info_results"] == {
        "wanted:123456": {
            "attempted": True,
            "completeness": None,
            "persisted": False,
            "status": "warning",
            "warning_code": "missing",
        }
    }


def test_screening_stage_rescreen_fetch_failure_keeps_incomplete_file_and_attempted_state(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    company_dir = tmp_path / "private/company_info"
    company_dir.mkdir(parents=True, exist_ok=True)
    company_file = company_dir / "goldenco.md"
    company_file.write_text(
        "# GoldenCo\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "---\n\n"
        "*출처:*\n- https://old.example.com\n",
        encoding="utf-8",
    )
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    _complete_jd = "# Backend\n\n## 자격 요건\n\n- Java 3년 이상\n"
    repository.create(
        JobRecord("wanted", "123456", "GoldenCo", "Backend"),
        jd_markdown=_complete_jd,
    )
    url = "https://www.wanted.co.kr/wd/123456"
    stage = JobsExtractionStage(repository=repository, http_client=FakeHttpClient())

    batch = stage.extract([url], dry_run=True, screening_only=True)

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )
    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        batch,
        dry_run=True,
        llm_timeout=1,
    )

    assert batch.company_contexts == {}
    assert "자격 요건" in batch.records[0].jd_markdown
    assert "자격 요건" in repository.get(JobKey("wanted", "123456")).jd_markdown
    assert company_file.read_text(encoding="utf-8").startswith("# GoldenCo\n\n## 기업 정보")
    assert result.item_ids == ("wanted:123456",)
    assert result.metadata["company_info_warnings"] == {
        "wanted:123456": _COMPANY_INFO_INCOMPLETE,
    }
    assert result.metadata["company_info_results"] == {
        "wanted:123456": {
            "attempted": True,
            "completeness": 50.0,
            "persisted": False,
            "status": "warning",
            "warning_code": "below_threshold",
        }
    }


@pytest.mark.parametrize(
    ("enrichment_result", "enrichment_error"),
    [
        (
            CompanyInfoEnrichmentResult(
                status="error",
                attempted=False,
                persisted=False,
                completeness=None,
                warning_code=None,
                file_path=None,
            ),
            None,
        ),
        (None, TimeoutError("company info writer lock timeout")),
        (None, OSError("company info storage failure")),
    ],
)
def test_screening_stage_turns_enrichment_failures_into_item_failures(
    tmp_path: Path,
    monkeypatch,
    enrichment_result: CompanyInfoEnrichmentResult | None,
    enrichment_error: Exception | None,
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Broken Co", "Backend"),
        jd_markdown="# JD\n",
    )

    def fake_enrich(self, context, *, dry_run=False, timeout=1.0):
        del self, context, dry_run, timeout
        if enrichment_error is not None:
            raise enrichment_error
        assert enrichment_result is not None
        return enrichment_result

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.CompanyEnrichmentService.enrich",
        fake_enrich,
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:1",),
            (record,),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Broken Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={},
                    fact_sources={},
                )
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ()
    assert result.metadata["failure_count"] == 1
    assert result.metadata["failures"] == [
        {
            "job_key": "wanted:1",
            "error_code": "company_info_failed",
            "error": "company info unavailable",
        }
    ]
    assert result.metadata["company_info_warnings"] == {}


def test_screening_stage_sanitizes_private_company_info_failure_details(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Secret Co", "Backend"),
        jd_markdown="# JD\n",
    )

    def fake_enrich(self, context, *, dry_run=False, timeout=1.0):
        del self, context, dry_run, timeout
        raise OSError("secret token sk-live-123 /Users/test/private/company_info/secret.md")

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.CompanyEnrichmentService.enrich",
        fake_enrich,
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:1",),
            (record,),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Secret Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={},
                    fact_sources={},
                )
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.metadata["failures"] == [
        {
            "job_key": "wanted:1",
            "error_code": "company_info_failed",
            "error": "company info unavailable",
        }
    ]
    assert "sk-live-123" not in json.dumps(result.metadata, ensure_ascii=False)
    assert "/Users/test/private/company_info/secret.md" not in json.dumps(
        result.metadata, ensure_ascii=False
    )


def test_screening_stage_logs_stable_company_info_failure_code(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Secret Co", "Backend"),
        jd_markdown="# JD\n",
    )

    def fake_enrich(self, context, *, dry_run=False, timeout=1.0):
        del self, context, dry_run, timeout
        raise OSError("secret token sk-live-123 /Users/test/private/company_info/secret.md")

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.CompanyEnrichmentService.enrich",
        fake_enrich,
    )
    caplog.set_level(logging.WARNING, logger="careerkit.jobs.application.automation")

    JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:1",),
            (record,),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Secret Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={},
                    fact_sources={},
                )
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert any(_COMPANY_INFO_FAILURE_CODE in message for message in caplog.messages)
    assert all("sk-live-123" not in message for message in caplog.messages)
    assert all("/Users/test/private/company_info/secret.md" not in message for message in caplog.messages)
    assert all("wanted:1" not in message for message in caplog.messages)


def test_screening_stage_logs_stable_company_info_failure_code_for_invalid_result(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "1", "Invalid Co", "Backend"),
        jd_markdown="# JD\n",
    )

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.CompanyEnrichmentService.enrich",
        lambda *args, **kwargs: CompanyInfoEnrichmentResult(
            status="error",
            attempted=False,
            persisted=False,
            completeness=None,
            warning_code=None,
            file_path=None,
        ),
    )
    caplog.set_level(logging.WARNING, logger="careerkit.jobs.application.automation")

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("wanted:1",),
            (record,),
            {},
            company_contexts={
                "wanted:1": CompanyEnrichmentContext(
                    platform="wanted",
                    item_id="wanted:1",
                    company_name="Invalid Co",
                    company_id=None,
                    source_url="https://www.wanted.co.kr/wd/1",
                    facts={},
                    fact_sources={},
                )
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.metadata["failures"] == [
        {
            "job_key": "wanted:1",
            "error_code": "company_info_failed",
            "error": "company info unavailable",
        }
    ]
    assert any(_COMPANY_INFO_FAILURE_CODE in message for message in caplog.messages)
    assert all("wanted:1" not in message for message in caplog.messages)
    assert all("Invalid Co" not in message for message in caplog.messages)


def test_screening_stage_groupby_search_parse_failure_does_not_abort_batch(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    first = repository.create(JobRecord("groupby", "1", "Alpha", "Backend"), jd_markdown="# One\n")
    second = repository.create(JobRecord("groupby", "2", "Beta", "Backend"), jd_markdown="# Two\n")
    calls: list[str] = []

    def fake_search(name: str, **kwargs):
        calls.append(name)
        if name == "Alpha":
            raise ValueError("bad nested search row")
        return None

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        fake_search,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("u1", "u2"),
            ("groupby:1", "groupby:2"),
            (first, second),
            {},
            company_contexts={
                "groupby:1": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:1",
                    company_name="Alpha",
                    company_id="1",
                    source_url="https://groupby.kr/positions/1",
                    facts={"industry": "IT", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/1",)},
                ),
                "groupby:2": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:2",
                    company_name="Beta",
                    company_id="2",
                    source_url="https://groupby.kr/positions/2",
                    facts={"industry": "IT", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/2",)},
                ),
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert calls == ["Alpha", "Beta"]
    assert result.item_ids == ("groupby:1", "groupby:2")
    assert result.metadata["failure_count"] == 0
    assert result.metadata["company_info_warnings"] == {
        "groupby:1": _COMPANY_INFO_INCOMPLETE,
        "groupby:2": _COMPANY_INFO_INCOMPLETE,
    }


def test_screening_stage_dry_run_enrichment_does_not_write_company_file(tmp_path: Path, monkeypatch) -> None:
    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("groupby", "456", "Dry Run Co", "Backend"),
        jd_markdown="# JD\n",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("groupby:456",),
            (record,),
            {},
            company_contexts={
                "groupby:456": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:456",
                    company_name="Dry Run Co",
                    company_id="456",
                    source_url="https://groupby.kr/positions/456",
                    facts={
                        "founded_year": 2020,
                        "employee_current": 40,
                        "employee_joined_1y": 8,
                        "employee_left_1y": 2,
                        "investment_round": "Series A",
                        "investment_total": 120.0,
                        "is_startup": True,
                    },
                    fact_sources={
                        "founded_year": ("https://groupby.kr/positions/456",),
                        "employee_current": ("https://groupby.kr/positions/456",),
                        "employee_joined_1y": ("https://groupby.kr/positions/456",),
                        "employee_left_1y": ("https://groupby.kr/positions/456",),
                        "investment_round": ("https://groupby.kr/positions/456",),
                        "investment_total": ("https://groupby.kr/positions/456",),
                        "is_startup": ("https://groupby.kr/positions/456",),
                    },
                )
            },
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert result.item_ids == ("groupby:456",)
    assert result.metadata["company_info_results"] == {
        "groupby:456": {
            "attempted": True,
            "completeness": 100.0,
            "persisted": False,
            "status": "ready",
            "warning_code": None,
        }
    }
    assert not (tmp_path / "private/company_info/dry-run-co.md").exists()


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


def test_screening_metadata_records_per_item_provider_telemetry(
    tmp_path: Path, monkeypatch
) -> None:
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                verdict="지원 추천",
                provider="claude",
                provider_attempts={"claude": ("ok",)},
                verdict_capped=False,
                downgraded=False,
                context_warning="claude: context near limit",
                published=True,
            ),
            _screening_result(
                verdict="지원 보류",
                provider="fallback",
                used_fallback=True,
                fallback_reason="ollama: network unavailable",
                provider_attempts={"claude": ("not logged in",), "ollama": ("network unavailable",)},
                verdict_capped=True,
                downgraded=True,
                context_warning="ollama: prompt near limit",
                published=True,
            ),
        ],
    )

    assert metadata["items"] == [
        {
            "job_key": "wanted:900",
            "provider": "claude",
            "verdict": "지원 추천",
            "verdict_capped": False,
            "downgraded": False,
            "published": True,
            "used_fallback": False,
            "fallback_reason": None,
            "provider_attempts": {"claude": ["ok"]},
            "context_warning": "claude: context near limit",
        },
        {
            "job_key": "wanted:901",
            "provider": "fallback",
            "verdict": "지원 보류",
            "verdict_capped": True,
            "downgraded": True,
            "published": True,
            "used_fallback": True,
            "fallback_reason": "ollama: network unavailable",
            "provider_attempts": {
                "claude": ["not logged in"],
                "ollama": ["network unavailable"],
            },
            "context_warning": "ollama: prompt near limit",
        },
    ]
    assert metadata["providers"] == {"claude": 1, "fallback": 1}
    assert metadata["fallback_count"] == 1
    assert metadata["context_warnings"] == 2


def test_screening_metadata_uses_legacy_defaults_and_excludes_failures_from_items(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "acme", "Acme Inc.")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "900", "Acme Inc.", "Backend"),
        jd_markdown="# JD\n",
    )
    pending = [
        SimpleNamespace(
            verdict="지원 추천",
            provider="claude",
            used_fallback=False,
            verdict_capped=False,
            downgraded=False,
            evidence_violations={},
            context_warning=None,
        ),
        ValueError("screening failed"),
    ]

    def fake_run_screening(**kwargs):
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening", fake_run_screening
    )
    batch = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url", "bad-url"),
            ("wanted:900", "wanted:901"),
            (
                record,
                StoredJobRecord(
                    record=JobRecord("wanted", "901", "Broken Co", "Backend"),
                    jd_markdown="# JD\n",
                ),
            ),
            {},
        ),
        dry_run=True,
        llm_timeout=1,
    )

    assert batch.metadata["items"] == [
        {
            "job_key": "wanted:900",
            "provider": "claude",
            "verdict": "지원 추천",
            "verdict_capped": False,
            "downgraded": False,
            "published": False,
            "used_fallback": False,
            "fallback_reason": None,
            "provider_attempts": {},
            "context_warning": None,
        }
    ]
    assert batch.metadata["failures"] == [{"job_key": "wanted:901", "error": "screening failed"}]
    assert batch.metadata["company_info_warnings"] == {"wanted:901": _COMPANY_INFO_MISSING}


def test_render_human_keeps_screening_items_out_of_bounded_summary() -> None:
    payload = {
        "stage": "screen",
        "screening": {
            "providers": {"claude": 1},
            "items": [
                {
                    "job_key": "wanted:1",
                    "provider": "claude",
                    "verdict": "지원 추천",
                }
            ],
        },
    }

    output = _render_json(payload, False)

    assert "screening={'providers': {'claude': 1}}" in output
    assert "wanted:1" not in output
    assert "items" not in next(line for line in output.splitlines() if line.startswith("screening="))


def test_atomic_write_json_keeps_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)

    from careerkit.jobs.application.automation import _atomic_write_json

    _atomic_write_json(path, {"ok": True})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_screening_metadata_sanitizes_private_provider_telemetry(
    tmp_path: Path, monkeypatch
) -> None:
    noisy_reason = (
        "API_SECRET=abcd1234\n"
        "/Users/test/private/file.md?token=super-secret-value&sig=abc\n"
        + ("x" * 400)
    )
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                provider="fallback",
                used_fallback=True,
                fallback_reason=noisy_reason,
                provider_attempts={"ollama": (noisy_reason,)},
            )
        ],
    )

    item = metadata["items"][0]
    attempt = item["provider_attempts"]["ollama"][0]

    assert "\n" not in item["fallback_reason"]
    assert "/Users/test/private" not in item["fallback_reason"]
    assert "super-secret-value" not in item["fallback_reason"]
    assert "?" not in item["fallback_reason"]
    assert len(item["fallback_reason"]) <= 243
    assert "\n" not in attempt
    assert "/Users/test/private" not in attempt
    assert "super-secret-value" not in attempt
    assert "?" not in attempt
    assert len(attempt) <= 243


def test_screening_metadata_redacts_lowercase_secret_assignments(
    tmp_path: Path, monkeypatch
) -> None:
    noisy_reason = (
        "token=abcd1234 secret=efgh5678 api_key=ijkl9012 access_key=mnop3456"
    )
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                provider="fallback",
                used_fallback=True,
                fallback_reason=noisy_reason,
                provider_attempts={"ollama": (noisy_reason,)},
            )
        ],
    )

    item = metadata["items"][0]
    attempt = item["provider_attempts"]["ollama"][0]

    assert item["fallback_reason"] == (
        "token=[redacted] secret=[redacted] api_key=[redacted] access_key=[redacted]"
    )
    assert attempt == item["fallback_reason"]


def test_screening_metadata_redacts_delimited_absolute_paths(
    tmp_path: Path, monkeypatch
) -> None:
    noisy_reason = (
        "cwd=/Users/test/private/project path=/Users/test/private/file.md?token=secret123"
    )
    metadata = _screened_batch(
        tmp_path,
        monkeypatch,
        [
            _screening_result(
                provider="fallback",
                used_fallback=True,
                fallback_reason=noisy_reason,
                provider_attempts={
                    "ollama": (
                        "detail(cwd=/Users/test/private/project) "
                        "[path=/Users/test/private/file.md?token=secret123]",
                    )
                },
            )
        ],
    )

    item = metadata["items"][0]
    attempt = item["provider_attempts"]["ollama"][0]

    assert item["fallback_reason"] == "cwd=[path] path=[path]"
    assert attempt == "detail(cwd=[path]) [path=[path]]"


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


def test_jobs_extraction_stage_saramin_deduplicates_exact_cross_source_items_only(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html=(
                        "<h2>담당업무</h2><ul><li>API 개발</li></ul>"
                        "<h2>지원자격</h2><ul><li>Python 경험</li><li>Python 경험</li></ul>"
                        "<h2>우대조건</h2><p>AWS 경험</p>"
                    ),
                    detail_pairs=(
                        ("담당업무", "API 개발<br>장애 대응"),
                        ("자격요건", "Python 경험<br>Python 경험 3년 이상"),
                        ("우대사항", "AWS 경험<br>테스트 자동화"),
                    ),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "주요 업무") == "- API 개발\n- 장애 대응"
    assert _section_body(markdown, "자격 요건") == "- Python 경험\n- Python 경험\n- Python 경험 3년 이상"
    assert _section_body(markdown, "우대사항") == "- AWS 경험\n- 테스트 자동화"


def test_jobs_extraction_stage_saramin_plain_text_headings_build_manifest(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html=(
                        "<div>모집분야</div>"
                        "<div>📋 주요업무</div><div>• API 개발</div>"
                        "<div>📋 자격요건</div><div>• Python 경험</div>"
                        "<div>🏠 근무조건</div><div>• 정규직</div>"
                    ),
                    detail_pairs=(),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "주요 업무") == "- API 개발"
    assert _section_body(markdown, "자격 요건") == "- Python 경험"
    manifest = extract_requirement_manifest(markdown)
    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("API 개발", RequirementKind.MAIN_DUTY),
        ("Python 경험", RequirementKind.REQUIRED),
    ]


def test_jobs_extraction_stage_saramin_cross_source_dedup_ignores_bullet_formatting_only(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html="<h2>자격요건</h2><p>Python 경험</p>",
                    detail_pairs=(("자격요건", "Python 경험"),),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "자격 요건") == "Python 경험"

def test_jobs_extraction_stage_saramin_uses_detail_fallback_and_keeps_mixed_requirement_prose_ambiguous(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html=(
                        "<h2>주요업무</h2><ul><li>서비스 운영</li></ul>"
                        "<h2>자격요건</h2><li>SQL</li><p>문서화 역량</p>"
                    ),
                    detail_pairs=(("우대사항", "커뮤니케이션"),),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "우대사항") == "- 커뮤니케이션"
    manifest = extract_requirement_manifest(markdown)

    assert [item.text for item in manifest.leaves if item.kind is RequirementKind.REQUIRED] == ["SQL"]
    assert manifest.ambiguous_qualifications is True


def test_jobs_extraction_stage_saramin_builds_manifest_in_source_order_and_without_main_duty_removes_only_duties(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html=(
                        "<h2>담당업무</h2><ul><li>서비스 개발</li><li>장애 대응</li></ul>"
                        "<h2>지원자격</h2><ul><li>Python</li></ul><p>협업 역량</p>"
                        "<h2>우대조건</h2><p>AWS</p>"
                        "<h2>기타</h2><p>팀 소개</p>"
                    ),
                    detail_pairs=(("자격요건", "FastAPI"),),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "포지션 소개") == "팀 소개"
    manifest = extract_requirement_manifest(markdown)

    assert [(item.text, item.kind) for item in manifest.parents] == [
        ("서비스 개발", RequirementKind.MAIN_DUTY),
        ("장애 대응", RequirementKind.MAIN_DUTY),
        ("Python", RequirementKind.REQUIRED),
        ("FastAPI", RequirementKind.REQUIRED),
        ("AWS", RequirementKind.PREFERRED),
    ]
    assert without_main_duty(manifest).parents == tuple(item for item in manifest.parents if item.kind is not RequirementKind.MAIN_DUTY)



def test_jobs_extraction_stage_saramin_does_not_use_long_recognized_only_body_as_introduction(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(
            text_by_url={
                detail_url: _saramin_detail_html(
                    job_id="123",
                    body_html=(
                        "<h2>자격요건</h2><ul>"
                        "<li>Python 경험 5년 이상 및 대규모 서비스 유지보수 경험</li>"
                        "<li>FastAPI 기반 API 설계 및 운영 경험과 성능 최적화 경험</li>"
                        "<li>테스트 자동화와 장애 대응 경험 및 운영 문서 작성 경험</li>"
                        "</ul>"
                        "<h2>우대사항</h2><ul>"
                        "<li>AWS 운영 경험과 모니터링 도구 활용 경험</li>"
                        "<li>대용량 트래픽 처리 경험과 협업 프로세스 개선 경험</li>"
                        "</ul>"
                    ),
                    detail_pairs=(),
                )
            }
        ),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    markdown = batch.records[0].jd_markdown
    assert _section_body(markdown, "포지션 소개") == "정보 없음"
    assert _section_body(markdown, "자격 요건") == (
        "- Python 경험 5년 이상 및 대규모 서비스 유지보수 경험\n"
        "- FastAPI 기반 API 설계 및 운영 경험과 성능 최적화 경험\n"
        "- 테스트 자동화와 장애 대응 경험 및 운영 문서 작성 경험"
    )


def test_jobs_extraction_stage_saramin_builds_company_context_from_detail_identifier(tmp_path: Path) -> None:
    repository = JDRecordRepository(tmp_path / "records")
    url = "https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx=123"
    detail_url = "https://m.saramin.co.kr/job-search/view?rec_idx=123"
    detail_html = (
        _saramin_detail_html(
            job_id="123",
            body_html="<h2>자격요건</h2><p>Python</p>",
            detail_pairs=(),
        )
        + '<a href="/job-search/company-info-view?csn=Q1NOPLUS123==">company</a>'
    )
    stage = JobsExtractionStage(
        repository=repository,
        http_client=FakeHttpClient(text_by_url={detail_url: detail_html}),
    )

    batch = stage.extract([url], dry_run=True, screening_only=False)

    assert batch.company_contexts["saramin:123"] == CompanyEnrichmentContext(
        platform="saramin",
        item_id="saramin:123",
        company_name="테스트회사",
        company_id="Q1NOPLUS123==",
        source_url=detail_url,
        facts={},
        fact_sources={},
    )


# -- enrichment context fetch --


def test_enrichment_fetch_wanted_populates_salary_and_employees(monkeypatch):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="테스트랩스",
        industry="IT",
        founded_year=2018,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=4800,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )

    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:123456",
        company_name="테스트랩스",
        company_id="12345",
        source_url="https://www.wanted.co.kr/wd/123456",
        facts={},
        fact_sources={},
    )
    assert ctx.item_id == "wanted:123456"
    assert ctx.source_url == "https://www.wanted.co.kr/wd/123456"
    result = _enrichment_context_with_fetched_facts(ctx)
    assert result.facts["avg_salary"] == 4800
    assert result.facts["employee_current"] == 24
    assert result.facts["employee_joined_1y"] == 15
    assert result.facts["employee_left_1y"] == 13
    assert result.facts["founded_year"] == 2018
    assert "https://www.wanted.co.kr/company/12345" in result.fact_sources["avg_salary"]


def test_enrichment_fetch_wanted_returns_unchanged_on_failure(monkeypatch):
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: (_ for _ in ()).throw(ValueError("not found")),
    )
    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:999",
        company_name="없는회사",
        company_id="999",
        source_url="https://www.wanted.co.kr/wd/999",
        facts={},
        fact_sources={},
    )
    result = _enrichment_context_with_fetched_facts(ctx)
    assert result.facts == {}


def test_enrichment_fetch_wanted_returns_unchanged_on_unsafe_detail(monkeypatch):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="테스트랩스",
        industry="IT|보안",
        founded_year=2018,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=4800,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )
    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:999",
        company_name="테스트랩스",
        company_id="12345",
        source_url="https://www.wanted.co.kr/wd/999",
        facts={},
        fact_sources={},
    )

    result = _enrichment_context_with_fetched_facts(ctx)

    assert result == ctx


def test_enrichment_fetch_wanted_returns_unchanged_on_invalid_metrics(monkeypatch):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="테스트랩스",
        industry="IT",
        founded_year=1799,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=4800,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )
    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:999",
        company_name="테스트랩스",
        company_id="12345",
        source_url="https://www.wanted.co.kr/wd/999",
        facts={},
        fact_sources={},
    )

    result = _enrichment_context_with_fetched_facts(ctx)

    assert result == ctx


def test_enrichment_fetch_groupby_keeps_original_without_wanted_discovery(monkeypatch):
    called = []

    def record_search(name, **kwargs):
        called.append((name, kwargs))
        return None

    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        record_search,
        raising=False,
    )
    ctx = CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id="12345",
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "investment_round": "Series A",
            "is_startup": True,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )
    result = _enrichment_context_with_fetched_facts(ctx)
    assert result == CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id=None,
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "investment_round": "Series A",
            "is_startup": True,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )
    assert called == [("테스트스타트업", {"verify_industry": "헬스케어", "verify_location": "서울"})]


def test_enrichment_fetch_groupby_discovers_wanted_company_and_merges_only_missing_facts(
    monkeypatch,
):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="테스트스타트업",
        industry="헬스케어",
        founded_year=2018,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=4800,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda name, **kwargs: calls.append(
            (name, kwargs.get("verify_industry", ""), kwargs.get("verify_location", ""))
        )
        or 12345,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )

    ctx = CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id="12345",
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "investment_round": "Series A",
            "is_startup": True,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )

    result = _enrichment_context_with_fetched_facts(ctx)

    assert calls == [("테스트스타트업", "헬스케어", "서울")]
    assert result.company_id is None
    assert result.source_url == "https://groupby.kr/positions/12345"
    assert result.facts["employee_current"] == 30
    assert result.facts["industry"] == "헬스케어"
    assert result.facts["location"] == "서울"
    assert result.facts["investment_round"] == "Series A"
    assert result.facts["founded_year"] == 2018
    assert result.facts["avg_salary"] == 4800
    assert result.facts["employee_joined_1y"] == 15
    assert result.facts["employee_left_1y"] == 13
    assert result.facts["revenue"] == 38.3
    assert result.fact_sources["avg_salary"] == ("https://www.wanted.co.kr/company/12345",)
    assert result.fact_sources["revenue"] == ("https://www.wanted.co.kr/company/12345",)


def test_enrichment_fetch_groupby_returns_sanitized_context_on_detail_mismatch(monkeypatch):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="다른회사",
        industry="헬스케어",
        founded_year=2018,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=4800,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda name, **kwargs: 12345,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )

    ctx = CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id="12345",
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )

    result = _enrichment_context_with_fetched_facts(ctx)

    assert result == CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id=None,
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )


def test_enrichment_fetch_groupby_returns_sanitized_context_on_invalid_metrics(monkeypatch):
    from careerkit.jobs.adapters.platforms import wanted as wanted_mod

    fake_info = wanted_mod.WantedCompanyInfo(
        company_id=12345,
        name="테스트스타트업",
        industry="헬스케어",
        founded_year=1799,
        location="서울 강남구",
        employee_count=24,
        avg_salary_manwon=1_000_001,
        hired_1y=15,
        left_1y=13,
        total_sales_eok=38.3,
        sales_year="2023",
        tags=(),
        description="",
        homepage="",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda name, **kwargs: 12345,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: fake_info,
    )

    ctx = CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id="12345",
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )

    result = _enrichment_context_with_fetched_facts(ctx)

    assert result == CompanyEnrichmentContext(
        platform="groupby",
        item_id="groupby:12345",
        company_name="테스트스타트업",
        company_id=None,
        source_url="https://groupby.kr/positions/12345",
        facts={
            "employee_current": 30,
            "industry": "헬스케어",
            "location": "서울",
        },
        fact_sources={"employee_current": ("https://groupby.kr/positions/12345",)},
    )


def test_screening_stage_groupby_ready_file_skips_wanted_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    _write_valid_company_info(tmp_path, "ready-groupby", "Ready GroupBy")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("groupby", "1", "Ready GroupBy", "Backend"),
        jd_markdown="# JD\n",
    )
    search_calls: list[str] = []
    fetch_calls: list[str] = []
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda *args, **kwargs: search_calls.append("search"),
        raising=False,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda *args, **kwargs: fetch_calls.append("fetch"),
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("url",),
            ("groupby:1",),
            (record,),
            {},
            company_contexts={
                "groupby:1": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:1",
                    company_name="Ready GroupBy",
                    company_id="1",
                    source_url="https://groupby.kr/positions/1",
                    facts={"industry": "AI", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/1",)},
                )
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("groupby:1",)
    assert search_calls == []
    assert fetch_calls == []


def test_screening_stage_groupby_persists_ready_file_after_corroborated_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    from careerkit.jobs.adapters.platforms.wanted import WantedCompanyInfo

    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("groupby", "10", "Proof Co", "Backend"),
        jd_markdown="# JD\n",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda *args, **kwargs: 12345,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda *args, **kwargs: WantedCompanyInfo(
            company_id=12345,
            name="Proof Co",
            industry="IT",
            founded_year=2020,
            location="서울 강남구",
            employee_count=30,
            avg_salary_manwon=4800,
            hired_1y=5,
            left_1y=1,
            total_sales_eok=38.3,
            sales_year="2024",
            tags=(),
            description="",
            homepage="",
        ),
    )
    captured: dict[str, object] = {}

    def fake_run_screening(**kwargs):
        captured["company_file"] = kwargs["company_file"]
        return _screening_result()

    monkeypatch.setattr("careerkit.jobs.application.automation.run_screening", fake_run_screening)

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("u1",),
            ("groupby:10",),
            (record,),
            {},
            company_contexts={
                "groupby:10": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:10",
                    company_name="Proof Co",
                    company_id="10",
                    source_url="https://groupby.kr/positions/10",
                    facts={"industry": "IT", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/10",)},
                )
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    file_path = tmp_path / "private/company_info/proof-co.md"
    saved = file_path.read_text(encoding="utf-8")
    lookup = CompanyInfoService(workspace=workspace).inspect("Proof Co")

    assert result.item_ids == ("groupby:10",)
    assert captured["company_file"] == file_path
    assert result.metadata["company_info_results"]["groupby:10"]["status"] == "ready"
    assert "https://groupby.kr/positions/10" in saved
    assert "https://www.wanted.co.kr/company/12345" in saved
    assert lookup.status == "ready"
    assert lookup.digest is not None


def test_screening_stage_groupby_miss_preserves_incomplete_file_warning(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = _make_workspace(tmp_path)
    company_dir = tmp_path / "private/company_info"
    company_dir.mkdir(parents=True, exist_ok=True)
    company_file = company_dir / "proof-co.md"
    original = (
        "# Proof Co\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "---\n\n"
        "*출처:*\n- https://groupby.kr/positions/10\n"
    )
    company_file.write_text(original, encoding="utf-8")
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("groupby", "10", "Proof Co", "Backend"),
        jd_markdown="# JD\n",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("u1",),
            ("groupby:10",),
            (record,),
            {},
            company_contexts={
                "groupby:10": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:10",
                    company_name="Proof Co",
                    company_id="10",
                    source_url="https://groupby.kr/positions/10",
                    facts={"industry": "IT", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/10",)},
                )
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("groupby:10",)
    assert result.metadata["company_info_warnings"] == {
        "groupby:10": _COMPANY_INFO_INCOMPLETE,
    }
    assert result.metadata["company_info_results"]["groupby:10"]["attempted"] is True
    saved = company_file.read_text(encoding="utf-8")
    assert "https://www.wanted.co.kr/company/" not in saved
    assert "https://groupby.kr/positions/10" in saved


def test_screening_stage_groupby_incomplete_rerun_discovers_ready_file(
    tmp_path: Path, monkeypatch
) -> None:
    from careerkit.jobs.adapters.platforms.wanted import WantedCompanyInfo

    workspace = _make_workspace(tmp_path)
    company_dir = tmp_path / "private/company_info"
    company_dir.mkdir(parents=True, exist_ok=True)
    company_file = company_dir / "proof-co.md"
    company_file.write_text(
        "# Proof Co\n\n"
        "## 기업 정보\n\n"
        "| 항목 | 내용 |\n|------|------|\n"
        "| 설립 | 2020년 |\n\n"
        "---\n\n"
        "*출처:*\n- https://groupby.kr/positions/10\n",
        encoding="utf-8",
    )
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("groupby", "10", "Proof Co", "Backend"),
        jd_markdown="# JD\n",
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_search_company_id",
        lambda *args, **kwargs: 12345,
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda *args, **kwargs: WantedCompanyInfo(
            company_id=12345,
            name="Proof Co",
            industry="IT",
            founded_year=2020,
            location="서울 강남구",
            employee_count=30,
            avg_salary_manwon=4800,
            hired_1y=5,
            left_1y=1,
            total_sales_eok=38.3,
            sales_year="2024",
            tags=(),
            description="",
            homepage="",
        ),
    )
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    result = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        ExtractionBatch(
            ("u1",),
            ("groupby:10",),
            (record,),
            {},
            company_contexts={
                "groupby:10": CompanyEnrichmentContext(
                    platform="groupby",
                    item_id="groupby:10",
                    company_name="Proof Co",
                    company_id="10",
                    source_url="https://groupby.kr/positions/10",
                    facts={"industry": "IT", "location": "서울"},
                    fact_sources={"industry": ("https://groupby.kr/positions/10",)},
                )
            },
        ),
        dry_run=False,
        llm_timeout=1,
    )

    assert result.item_ids == ("groupby:10",)
    assert result.metadata["company_info_warnings"] == {}
    assert result.metadata["company_info_results"]["groupby:10"]["status"] == "ready"
    assert company_file.read_text(encoding="utf-8").count("## 매출 정보") == 1


def test_screening_stage_sync_failure_can_leave_valid_new_file_and_next_run_compensates(
    tmp_path: Path, monkeypatch
) -> None:
    from careerkit.jobs.application import company_info as company_info_mod

    workspace = _make_workspace(tmp_path)
    repository = JDRecordRepository(tmp_path / "private/jd/records")
    record = repository.create(
        JobRecord("wanted", "10", "Retry Co", "Backend"),
        jd_markdown="# JD\n",
    )
    sync_state = {"raised": False}

    original_fsync_directory = company_info_mod._fsync_directory

    def fail_once(path: Path) -> None:
        if not sync_state["raised"]:
            sync_state["raised"] = True
            raise OSError("sync failed")
        original_fsync_directory(path)

    monkeypatch.setattr(company_info_mod, "_fsync_directory", fail_once)
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.run_screening",
        lambda **kwargs: _screening_result(),
    )

    extraction = ExtractionBatch(
        ("u1",),
        ("wanted:10",),
        (record,),
        {},
        company_contexts={
            "wanted:10": CompanyEnrichmentContext(
                platform="wanted",
                item_id="wanted:10",
                company_name="Retry Co",
                company_id=None,
                source_url="https://www.wanted.co.kr/wd/10",
                facts={"founded_year": 2020, "employee_current": 30},
                fact_sources={
                    "founded_year": ("https://www.wanted.co.kr/wd/10",),
                    "employee_current": ("https://www.wanted.co.kr/wd/10",),
                },
            )
        },
    )

    failed = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        extraction,
        dry_run=False,
        llm_timeout=1,
    )
    file_path = tmp_path / "private/company_info/retry-co.md"
    assert failed.item_ids == ()
    assert failed.metadata["failures"] == [
        {
            "job_key": "wanted:10",
            "error_code": "company_info_failed",
            "error": "company info unavailable",
        }
    ]
    assert CompanyInfoService(workspace=workspace).inspect("Retry Co").status == "ready"
    assert file_path.exists()

    retried = JobsScreeningStage(workspace=workspace, repository=repository).screen(
        extraction,
        dry_run=False,
        llm_timeout=1,
    )

    assert retried.item_ids == ("wanted:10",)
    assert retried.metadata["company_info_results"]["wanted:10"]["attempted"] is False


def test_enrichment_fetch_wanted_skips_when_facts_already_present(monkeypatch):
    called = []
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: called.append(cid),
    )
    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:999",
        company_name="이미보강됨",
        company_id="999",
        source_url="https://www.wanted.co.kr/wd/999",
        facts={"industry": "IT"},
        fact_sources={"industry": ("https://example.com",)},
    )
    result = _enrichment_context_with_fetched_facts(ctx)
    assert result.facts == {"industry": "IT"}
    assert called == []


def test_enrichment_fetch_wanted_skips_when_no_company_id(monkeypatch):
    called = []
    monkeypatch.setattr(
        "careerkit.jobs.application.automation.wanted_company_http",
        lambda cid, **kw: called.append(cid),
    )
    ctx = CompanyEnrichmentContext(
        platform="wanted",
        item_id="wanted:999",
        company_name="아이디없음",
        company_id=None,
        source_url="https://www.wanted.co.kr/wd/999",
        facts={},
        fact_sources={},
    )
    result = _enrichment_context_with_fetched_facts(ctx)
    assert result.facts == {}
    assert called == []


# -- legacy JD re-extraction --


from careerkit.jobs.application.automation import _has_assessable_requirements


def test_has_assessable_requirements_returns_true_for_complete_jd():
    jd = (
        "# 시니어 백엔드\n\n"
        "## 자격 요건\n\n"
        "- Python 5년 이상\n"
        "- AWS 운영 경험\n"
    )
    assert _has_assessable_requirements(jd) is True


def test_has_assessable_requirements_returns_false_for_stub_jd():
    jd = "# 포지션명\n\n- **URL**: https://example.com\n- **사유**: 경력 미달\n"
    assert _has_assessable_requirements(jd) is False


def test_has_assessable_requirements_returns_false_for_empty():
    assert _has_assessable_requirements("") is False


def test_resolve_existing_re_extracts_stub_jd(tmp_path: Path, monkeypatch):
    from careerkit.jobs.domain.model import JobRecord

    repo = JDRecordRepository(tmp_path / "records")
    stub_jd = "# 테스트 포지션\n\n- **URL**: https://www.wanted.co.kr/wd/999999\n- **사유**: stub\n"
    repo.create(
        JobRecord(platform="wanted", job_id="999999", company="테스트회사", position="테스트", source_url="https://www.wanted.co.kr/wd/999999"),
        jd_markdown=stub_jd,
    )

    class FakeHttp:
        def request_text(self, url, **kw):
            return _make_wanted_next_data_html(
                company_name="테스트회사",
                position="시니어 백엔드",
                requirements="- Python 5년 이상\n- AWS 운영 경험",
            )
        def request_json(self, url, **kw):
            raise NotImplementedError

    stage = JobsExtractionStage(repository=repo, http_client=FakeHttp())
    result = stage._resolve_existing("https://www.wanted.co.kr/wd/999999")
    assert _has_assessable_requirements(result.jd_markdown) is True
    assert "Python" in result.jd_markdown


def test_resolve_existing_keeps_complete_jd(tmp_path: Path):
    from careerkit.jobs.domain.model import JobRecord

    repo = JDRecordRepository(tmp_path / "records")
    complete_jd = (
        "# 백엔드 엔지니어\n\n"
        "## 자격 요건\n\n"
        "- Java 3년 이상\n"
        "- Spring Boot 경험\n"
    )
    repo.create(
        JobRecord(platform="wanted", job_id="888888", company="좋은회사", position="백엔드", source_url="https://www.wanted.co.kr/wd/888888"),
        jd_markdown=complete_jd,
    )

    class FakeHttp:
        def request_text(self, url, **kw):
            raise AssertionError("should not re-extract")
        def request_json(self, url, **kw):
            raise NotImplementedError

    stage = JobsExtractionStage(repository=repo, http_client=FakeHttp())
    result = stage._resolve_existing("https://www.wanted.co.kr/wd/888888")
    assert result.jd_markdown == complete_jd


def test_resolve_existing_returns_original_on_extract_failure(tmp_path: Path):
    from careerkit.jobs.domain.model import JobRecord

    repo = JDRecordRepository(tmp_path / "records")
    stub_jd = "# 스텁\n\n- **사유**: legacy\n"
    repo.create(
        JobRecord(platform="wanted", job_id="777777", company="실패회사", position="스텁", source_url="https://www.wanted.co.kr/wd/777777"),
        jd_markdown=stub_jd,
    )

    class FakeHttp:
        def request_text(self, url, **kw):
            raise OSError("network down")
        def request_json(self, url, **kw):
            raise NotImplementedError

    stage = JobsExtractionStage(repository=repo, http_client=FakeHttp())
    result = stage._resolve_existing("https://www.wanted.co.kr/wd/777777")
    assert result.jd_markdown == stub_jd


def _make_wanted_next_data_html(
    *, company_name: str, position: str, requirements: str
) -> str:
    payload = {
        "props": {
            "pageProps": {
                "initialData": {
                    "id": 999999,
                    "position": position,
                    "company": {
                        "company_id": 1,
                        "company_name": company_name,
                    },
                    "address": {"full_location": "서울"},
                    "intro": "",
                    "main_tasks": "- 서비스 개발",
                    "requirements": requirements,
                    "preferred_points": "",
                    "benefits": "",
                    "career": {"annual_from": 5, "annual_to": 100},
                    "status": "active",
                    "employment_type": "regular",
                },
            },
        },
    }
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
