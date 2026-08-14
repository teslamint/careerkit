from __future__ import annotations

import argparse
import json
import logging
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from careerkit.jobs.adapters.config_files import YamlConfigFileAdapter
from careerkit.jobs.adapters.http import HttpClient, HttpError, UrllibHttpClient
from careerkit.jobs.adapters.platforms.groupby import format_groupby_experience
from careerkit.jobs.adapters.platforms.remember import remember_company_http
from careerkit.jobs.adapters.platforms.saramin import extract_csn_from_html, saramin_company_http
from careerkit.jobs.adapters.platforms.wanted import (
    WantedCompanyInfo,
    wanted_company_http,
    wanted_company_is_valid,
    wanted_company_matches,
    wanted_search_company_id,
)
from careerkit.jobs.adapters.screening.cli_provider import LLMProvider
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, StoredJobRecord
from careerkit.jobs.application.company_enrichment import (
    CompanyEnrichmentContext,
    CompanyEnrichmentService,
    CompanyInfoEnrichmentResult,
)
from careerkit.jobs.application.maintenance import JobsMaintenanceService
from careerkit.jobs.application.company_info import CompanyInfoService
from careerkit.jobs.application.pipeline import IngestResult, JobsPipelineService
from careerkit.jobs.application.screening import run_screening
from careerkit.jobs.application.storage_migration import extract_job_id, get_platform_from_url
from careerkit.jobs.application.title_filter import (
    classify_non_backend_domain,
    has_domain_counter_indicator,
    is_level_exclusion,
    matched_title_exclusion,
    quick_filter_title,
    requirements_show_backend,
)
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus
from careerkit.jobs.domain.naming import slugify_company
from careerkit.workspace import WorkspacePaths

logger = logging.getLogger(__name__)

_COMPANY_INFO_MISSING = "company info file missing"
_COMPANY_INFO_INCOMPLETE = "company info completeness below 70"
_COMPANY_INFO_FAILURE_CODE = "company_info_failed"
_COMPANY_INFO_FAILURE_MESSAGE = "company info unavailable"

MAX_CANDIDATE_CONTEXT_CHARS = 60_000
MAX_CANDIDATE_FILE_CHARS = 4_000
MAX_TELEMETRY_DETAIL_CHARS = 240
PROFILE_CONTEXT_FILES = (
    "summary-job.md",
    "skills-job.md",
    "core-competencies.md",
)


@dataclass(frozen=True)
class AutomationRunResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class ExtractionBatch:
    urls: tuple[str, ...]
    item_ids: tuple[str, ...]
    records: tuple[StoredJobRecord, ...]
    metadata: dict[str, Any]
    failed_urls: tuple[str, ...] = ()
    company_contexts: dict[str, CompanyEnrichmentContext] = field(default_factory=dict)


@dataclass(frozen=True)
class ScreeningBatch:
    item_ids: tuple[str, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CompletionBatch:
    item_ids: tuple[str, ...]
    metadata: dict[str, Any]


class SearchPort(Protocol):
    def search(self, *, max_urls: int | None = None) -> Any: ...
    def persist_seen_job_keys(
        self, seen_job_keys: set[str], *, new_count: int | None = None
    ) -> None: ...


class ExtractionStagePort(Protocol):
    def extract(
        self,
        urls: Sequence[str],
        *,
        dry_run: bool,
        screening_only: bool,
    ) -> ExtractionBatch: ...


class ScreeningStagePort(Protocol):
    def screen(
        self,
        extraction: ExtractionBatch,
        *,
        dry_run: bool,
        llm_timeout: int,
        local_llm_timeout: int | None = None,
    ) -> ScreeningBatch: ...


class CompletionStagePort(Protocol):
    def complete(
        self,
        extraction: ExtractionBatch,
        screening: ScreeningBatch,
        *,
        dry_run: bool,
        no_classify: bool,
    ) -> CompletionBatch: ...


class ResumeStatePort(Protocol):
    def load_pending_urls(self) -> tuple[str, ...]: ...
    def save_pending_urls(self, urls: Sequence[str]) -> None: ...
    def clear_pending_urls(self) -> None: ...


class ResultArtifactPort(Protocol):
    def save(self, payload: Mapping[str, Any], *, returncode: int, error: str) -> str: ...
    def save_search_request(self, urls: Sequence[str]) -> str: ...


class AutomationService:
    def __init__(
        self,
        *,
        search_port: SearchPort,
        extraction_stage: ExtractionStagePort | None = None,
        screening_stage: ScreeningStagePort | None = None,
        completion_stage: CompletionStagePort | None = None,
        resume_state: ResumeStatePort | None = None,
        result_store: ResultArtifactPort | None = None,
    ) -> None:
        self.search_port = search_port
        self.extraction_stage = extraction_stage
        self.screening_stage = screening_stage
        self.completion_stage = completion_stage
        self.resume_state = resume_state
        self.result_store = result_store

    def run(self, operation: str, args: Sequence[str]) -> AutomationRunResult:
        if operation == "auto":
            return self._run_auto(args)
        raise ValueError(f"unsupported run operation: {operation}")

    def _run_auto(self, args: Sequence[str]) -> AutomationRunResult:
        parsed = _build_auto_parser().parse_args(list(args))
        logger.debug("auto args: %s", vars(parsed))
        urls: tuple[str, ...]
        payload: dict[str, Any] = {"stage": "start"}
        resume_remainder: tuple[str, ...] = ()

        def finish(returncode: int, *, stderr: str = "") -> AutomationRunResult:
            if self.result_store is not None and not parsed.dry_run:
                payload["result_path"] = self.result_store.save(
                    payload,
                    returncode=returncode,
                    error=stderr,
                )
            return AutomationRunResult(
                returncode=returncode,
                stdout=_render_json(payload, parsed.json_mode) if returncode == 0 else "",
                stderr=stderr,
            )

        try:
            if parsed.resume:
                if self.resume_state is None:
                    return finish(
                        2,
                        stderr="career-jobs run auto --resume requires a resume state port.",
                    )
                urls = self.resume_state.load_pending_urls()
                payload = {
                    "mode": "resume",
                    "url_count": len(urls),
                }
            elif parsed.from_urls is not None:
                source = parsed.from_urls
                urls = _read_urls(source)
                payload = {
                    "mode": "from_urls",
                    "input_path": str(source),
                    "url_count": len(urls),
                }
            else:
                result = self.search_port.search(max_urls=parsed.max_urls)
                postings = tuple(result.postings)
                selected = postings[: parsed.max_urls] if parsed.max_urls is not None else postings
                urls = tuple(candidate.url for candidate in selected)
                if not parsed.dry_run:
                    omitted_keys = {candidate.seen_key for candidate in postings[len(selected) :]}
                    self.search_port.persist_seen_job_keys(
                        result.updated_seen_job_keys - omitted_keys,
                        new_count=len(selected),
                    )
                payload = {
                    "mode": "search",
                    "url_count": len(urls),
                    "counts": {
                        "total_found": result.total_found,
                        "returned": len(selected),
                        "filtered_out": result.filtered_out,
                        "duplicates": result.duplicates,
                    },
                    "diagnostics": list(result.diagnostics),
                    "capabilities": result.capabilities,
                }

            if parsed.max_urls is not None and parsed.from_urls is not None:
                urls = urls[: parsed.max_urls]
                payload["url_count"] = len(urls)
            elif parsed.max_urls is not None and parsed.resume:
                resume_remainder = urls[parsed.max_urls :]
                urls = urls[: parsed.max_urls]
                payload["url_count"] = len(urls)

            logger.info("auto: mode=%s url_count=%d", payload["mode"], len(urls))
            logger.info("search complete: %d URLs", len(urls))

            if parsed.search_only:
                if self.result_store is not None and not parsed.dry_run:
                    payload["request_path"] = self.result_store.save_search_request(urls)
                payload["stage"] = "search"
                return finish(0)

            if self.resume_state is not None and not parsed.dry_run:
                pending_urls = (*urls, *resume_remainder) if parsed.resume else urls
                self.resume_state.save_pending_urls(pending_urls)

            if self.extraction_stage is None:
                return finish(
                    2,
                    stderr="career-jobs run auto requires an extraction stage port.",
                )

            extraction = self.extraction_stage.extract(
                urls,
                dry_run=parsed.dry_run,
                screening_only=parsed.screening_only,
            )
            payload["stage"] = "extract"
            payload["extracted_count"] = len(extraction.item_ids)
            payload["extraction"] = extraction.metadata
            logger.info("extraction complete: %d records", len(extraction.item_ids))

            if parsed.dry_run and not parsed.screening_only:
                return finish(0)

            if self.screening_stage is None:
                return finish(
                    2,
                    stderr="career-jobs run auto requires a screening stage port.",
                )
            screened = self.screening_stage.screen(
                extraction,
                dry_run=parsed.dry_run,
                llm_timeout=parsed.llm_timeout,
                local_llm_timeout=parsed.local_llm_timeout,
            )
            payload["stage"] = "screen"
            payload["screened_count"] = len(screened.item_ids)
            payload["screening"] = screened.metadata
            logger.info("screening complete: %d records", len(screened.item_ids))

            if self.completion_stage is None:
                return finish(
                    2,
                    stderr="career-jobs run auto requires a completion stage port for classification/status/summary.",
                )
            completed = self.completion_stage.complete(
                extraction,
                screened,
                dry_run=parsed.dry_run,
                no_classify=parsed.no_classify,
            )
            payload["stage"] = "complete"
            payload["completed_count"] = len(completed.item_ids)
            payload["completion"] = completed.metadata
            logger.info("completion complete: %d records", len(completed.item_ids))

            if self.resume_state is not None and not parsed.dry_run:
                failed_screening_ids = {
                    str(item.get("job_key"))
                    for item in screened.metadata.get("failures", [])
                    if isinstance(item, Mapping) and item.get("job_key")
                }
                warned_screening_ids = set(
                    screened.metadata.get("company_info_warnings", {}).keys()
                )
                retry_screening_ids = failed_screening_ids | warned_screening_ids
                failed_screening_urls = tuple(
                    url
                    for item_id, url in zip(extraction.item_ids, extraction.urls, strict=True)
                    if item_id in retry_screening_ids
                )
                pending_urls = (
                    *extraction.failed_urls,
                    *failed_screening_urls,
                    *resume_remainder,
                )
                if pending_urls:
                    self.resume_state.save_pending_urls(pending_urls)
                else:
                    self.resume_state.clear_pending_urls()
        except (FileNotFoundError, HttpError, RuntimeError, ValueError) as exc:
            return finish(2, stderr=str(exc))

        return finish(0)


class JobsResumeStateService:
    def __init__(self, *, workspace: WorkspacePaths) -> None:
        self.path = workspace.private_dir / "jd" / "runtime" / "auto" / "pending_urls.json"

    def load_pending_urls(self) -> tuple[str, ...]:
        if not self.path.exists():
            state_dir = self.path.parent / "state"
            state_files = sorted(
                state_dir.glob(".auto_state_*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for state_path in state_files:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                items = payload.get("items", payload) if isinstance(payload, dict) else {}
                if isinstance(items, dict):
                    urls = tuple(
                        str(item["url"])
                        for item in items.values()
                        if isinstance(item, dict)
                        and item.get("status") != "done"
                        and item.get("url")
                    )
                    if urls:
                        return urls
            return ()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            values = payload.get("urls")
            if isinstance(values, list):
                return tuple(str(item) for item in values)
        if isinstance(payload, list):
            return tuple(str(item) for item in payload)
        raise ValueError(f"invalid pending URL state: {self.path}")

    def save_pending_urls(self, urls: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, {"urls": list(urls)})

    def clear_pending_urls(self) -> None:
        self.path.unlink(missing_ok=True)


class JobsAutoResultService:
    def __init__(self, *, workspace: WorkspacePaths) -> None:
        self.workspace = workspace
        self.directory = workspace.jobs_runtime_dir / "auto" / "results"

    def save(self, payload: Mapping[str, Any], *, returncode: int, error: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.directory / f"auto_{run_id}.json"
        artifact = {
            "run_id": run_id,
            "timestamp": datetime.now().isoformat(),
            "returncode": returncode,
            "error": error,
            **dict(payload),
        }
        _atomic_write_json(path, artifact)
        try:
            return str(path.relative_to(self.workspace.root))
        except ValueError:
            return str(path)

    def save_search_request(self, urls: Sequence[str]) -> str:
        directory = self.workspace.jobs_runtime_dir / "search" / "requests"
        directory.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = directory / f"search_{run_id}.txt"
        _atomic_write_text(path, "\n".join(urls) + ("\n" if urls else ""))
        try:
            return str(path.relative_to(self.workspace.root))
        except ValueError:
            return str(path)


class JobsExtractionStage:
    def __init__(
        self,
        *,
        repository: JDRecordRepository,
        http_client: HttpClient | None = None,
    ) -> None:
        self.repository = repository
        self.http_client = http_client or UrllibHttpClient()

    def extract(
        self,
        urls: Sequence[str],
        *,
        dry_run: bool,
        screening_only: bool,
    ) -> ExtractionBatch:
        records: list[StoredJobRecord] = []
        company_contexts: dict[str, CompanyEnrichmentContext] = {}
        successful_urls: list[str] = []
        failed_urls: list[str] = []
        failures: list[dict[str, str]] = []
        duplicates: list[str] = []
        for index, url in enumerate(urls, 1):
            logger.debug("extracting: %s", url)
            try:
                key = _job_key_from_url(url)
                if not screening_only and key is not None and self.repository.find(key) is not None:
                    duplicates.append(f"{key.platform}:{key.job_id}")
                    continue
                if screening_only:
                    record = self._resolve_existing(url, dry_run=dry_run)
                    context = self._extract_context_for_url(url)
                else:
                    record, context = self._extract_url(url, dry_run=dry_run)
            except (FileNotFoundError, HttpError, RuntimeError, ValueError) as exc:
                key = _job_key_from_url(url)
                item_id = f"{key.platform}:{key.job_id}" if key is not None else f"item-{index}"
                failures.append({"item_id": item_id, "error": str(exc).replace(url, item_id)})
                failed_urls.append(url)
                continue
            records.append(record)
            item_id = f"{record.record.platform}:{record.record.job_id}"
            if context is not None:
                company_contexts[item_id] = context
            successful_urls.append(url)
        item_ids = tuple(f"{item.record.platform}:{item.record.job_id}" for item in records)
        metadata = {
            "mode": "screening_only" if screening_only else "extract",
            "item_ids": list(item_ids),
            "items": [
                {
                    "job_key": f"{item.record.platform}:{item.record.job_id}",
                    "company": item.record.company,
                    "position": item.record.position,
                }
                for item in records
            ],
            "platforms": sorted({item.record.platform for item in records}),
            "reused_existing_records": screening_only,
            "requested_count": len(urls),
            "failure_count": len(failures),
            "failures": failures,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
        }
        return ExtractionBatch(
            urls=tuple(successful_urls),
            item_ids=item_ids,
            records=tuple(records),
            metadata=metadata,
            failed_urls=tuple(failed_urls),
            company_contexts=company_contexts,
        )

    def _resolve_existing(self, url: str, *, dry_run: bool = False) -> StoredJobRecord:
        key = _job_key_from_url(url)
        if key is None:
            raise ValueError(f"unsupported or invalid job URL: {url}")
        stored = self.repository.get(key)
        if _has_assessable_requirements(stored.jd_markdown):
            return stored
        try:
            refreshed, _context = self._extract_url(url, dry_run=dry_run)
        except (HttpError, OSError, RuntimeError, ValueError):
            return stored
        return refreshed

    def _extract_url(
        self, url: str, *, dry_run: bool
    ) -> tuple[StoredJobRecord, CompanyEnrichmentContext | None]:
        key = _job_key_from_url(url)
        if key is None:
            raise ValueError(f"unsupported or invalid job URL: {url}")
        if key.platform == "wanted":
            company, position, markdown, context = self._extract_wanted(url, key.job_id)
        elif key.platform == "remember":
            company, position, markdown, context = self._extract_remember(url, key.job_id)
        elif key.platform == "groupby":
            company, position, markdown, context = self._extract_groupby(url, key.job_id)
        elif key.platform == "saramin":
            company, position, markdown, context = self._extract_saramin(url, key.job_id)
        else:
            raise ValueError(
                "career-jobs run auto currently supports canonical extraction for wanted, remember, groupby, and saramin URLs only; "
                f"got {key.platform}:{key.job_id}"
            )
        return (
            _save_or_preview_record(
                repository=self.repository,
                key=key,
                company=company,
                position=position,
                source_url=url,
                jd_markdown=markdown,
                dry_run=dry_run,
            ),
            context,
        )

    def _extract_context_for_url(self, url: str) -> CompanyEnrichmentContext | None:
        key = _job_key_from_url(url)
        if key is None:
            return None
        try:
            if key.platform == "wanted":
                return self._extract_wanted(url, key.job_id)[3]
            if key.platform == "remember":
                return self._extract_remember(url, key.job_id)[3]
            if key.platform == "groupby":
                return self._extract_groupby(url, key.job_id)[3]
            if key.platform == "saramin":
                return self._extract_saramin(url, key.job_id)[3]
        except (AssertionError, HttpError, OSError, RuntimeError, ValueError):
            return None
        return None

    def _extract_wanted(
        self, url: str, job_id: str
    ) -> tuple[str, str, str, CompanyEnrichmentContext]:
        html = self.http_client.request_text(url)
        data = _extract_next_data(html)
        try:
            job = data["props"]["pageProps"]["initialData"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid Wanted payload for {url}") from exc
        position = _normalize_text(job.get("position", "")) or f"wanted-{job_id}"
        company_info = job.get("company") or {}
        company = _normalize_text(company_info.get("company_name", "")) or "unknown-company"
        address = job.get("address") or {}
        location = _normalize_text(address.get("full_location", ""))
        introduction = str(job.get("intro", "") or "")
        main_duties = str(job.get("main_tasks", "") or "")
        return (
            company,
            position,
            _format_jd_markdown(
                title=position,
                company=company,
                experience=_format_wanted_experience(job),
                location=location,
                url=url,
                introduction=introduction,
                main_duties=main_duties,
                requirements=str(job.get("requirements", "") or ""),
                preferred=str(job.get("preferred_points", "") or ""),
                benefits=str(job.get("benefits", "") or ""),
                source="Wanted",
            ),
            CompanyEnrichmentContext(
                platform="wanted",
                item_id=f"wanted:{job_id}",
                company_name=company,
                company_id=str(company_info["company_id"]) if company_info.get("company_id") is not None else None,
                source_url=url,
                facts={},
                fact_sources={},
            ),
        )

    def _extract_remember(
        self, url: str, job_id: str
    ) -> tuple[str, str, str, CompanyEnrichmentContext]:
        html = self.http_client.request_text(url)
        data = _extract_next_data(html)
        try:
            posting = data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid Remember payload for {url}") from exc
        organization = posting.get("organization") or {}
        company_id = _normalize_text(str(organization.get("id", "") or ""))
        company = _normalize_text((organization.get("name", "") or "").replace("(주)", "").replace("(주 )", "")) or "unknown-company"
        position = _normalize_text(posting.get("title", "")) or f"remember-{job_id}"
        benefit_parts = _remember_operator_details(posting)
        benefits = "\n\n".join(benefit_parts)
        introduction = str(posting.get("introduction", "") or "")
        main_duties = str(posting.get("jobDescription", "") or "")
        return (
            company,
            position,
            _format_jd_markdown(
                title=position,
                company=company,
                experience=_format_remember_experience(posting),
                location=_format_remember_address(posting),
                url=url,
                introduction=introduction,
                main_duties=main_duties,
                requirements=str(posting.get("qualifications", "") or ""),
                preferred=str(posting.get("preferredQualifications", "") or ""),
                benefits=benefits,
                source="Remember",
            ),
            CompanyEnrichmentContext(
                platform="remember",
                item_id=f"remember:{job_id}",
                company_name=company,
                company_id=company_id or None,
                source_url=url,
                facts={},
                fact_sources={},
            ),
        )

    def _extract_groupby(
        self, url: str, job_id: str
    ) -> tuple[str, str, str, CompanyEnrichmentContext]:
        payload = self.http_client.request_json(f"https://api.groupby.kr/startup-positions/{job_id}")
        try:
            if payload.get("status") != 200:
                raise ValueError(f"GroupBy API status {payload.get('status')}")
            data = payload["data"]
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError(f"invalid GroupBy payload for {url}") from exc
        position = _normalize_text(data.get("name", "")) or f"groupby-{job_id}"
        startup = data.get("startup") or {}
        company = _normalize_text(startup.get("name", "")) or "unknown-company"
        location = _normalize_text(data.get("address", ""))
        if not location:
            loc_obj = data.get("location") or {}
            location = _normalize_text(loc_obj.get("name", ""))
        if not location:
            location = _normalize_text(startup.get("location", ""))
        benefits = _html_to_text(data.get("hiringProcess", ""))
        tech_stacks = data.get("techStacks")
        if isinstance(tech_stacks, list) and tech_stacks:
            stack_names = [
                _normalize_text(item.get("name", ""))
                for item in tech_stacks
                if isinstance(item, dict) and _normalize_text(item.get("name", ""))
            ]
            if stack_names:
                extra = f"기술스택: {', '.join(stack_names)}"
                benefits = f"{benefits}\n\n{extra}" if benefits else extra
        company_context = _format_groupby_company_context(startup, data)
        task = _html_to_text(data.get("task", ""))
        raw_areas = startup.get("serviceAreas", ())
        industry_values = tuple(
            _normalize_text(item.get("name", "") if isinstance(item, dict) else str(item))
            for item in (raw_areas if isinstance(raw_areas, list) else ())
            if _normalize_text(item.get("name", "") if isinstance(item, dict) else str(item))
        )
        facts: dict[str, object] = {
            "employee_current": _safe_int(_first_groupby_value("memberCount", startup, data)),
            "investment_round": _normalize_text(_first_groupby_value("fundingRound", startup, data)),
            "is_startup": True,
        }
        if industry_values:
            facts["industry"] = ", ".join(industry_values)
        if location:
            facts["location"] = location
        fact_sources = {
            field: (url,)
            for field, value in facts.items()
            if value not in (None, "", ())
        }
        return (
            company,
            position,
            _format_jd_markdown(
                title=position,
                company=company,
                experience=format_groupby_experience(data),
                location=location,
                url=url,
                introduction=company_context,
                main_duties=task,
                requirements=_html_to_text(data.get("qualification", "")),
                preferred=_html_to_text(data.get("preferred", "")),
                benefits=benefits,
                source="GroupBy",
            ),
            CompanyEnrichmentContext(
                platform="groupby",
                item_id=f"groupby:{job_id}",
                company_name=company,
                company_id=job_id,
                source_url=url,
                facts={
                    key: value for key, value in facts.items() if value not in (None, "", ())
                },
                fact_sources=fact_sources,
            ),
        )

    def _extract_saramin(
        self, url: str, job_id: str
    ) -> tuple[str, str, str, CompanyEnrichmentContext]:
        from careerkit.jobs.adapters.platforms.saramin import (
            SARAMIN_MOBILE_BASE,
            extract_company_from_detail,
            extract_detail_fields,
            extract_detail_sections,
            extract_jd_body,
            extract_jd_sections,
            extract_position_from_detail,
        )

        detail_url = f"{SARAMIN_MOBILE_BASE}/job-search/view?rec_idx={job_id}"
        html = self.http_client.request_text(detail_url)
        csn = extract_csn_from_html(html)
        company = _normalize_text(extract_company_from_detail(html)) or "unknown-company"
        position = _normalize_text(extract_position_from_detail(html)) or f"saramin-{job_id}"
        fields = extract_detail_fields(html)
        jd_body = extract_jd_body(html, job_id)
        body_sections = extract_jd_sections(html, job_id)
        detail_sections = extract_detail_sections(html)
        experience = fields.get("경력", "") or fields.get("경력조건", "")
        location = fields.get("지역", "")
        introduction = _merge_saramin_intro(body_sections.introduction)
        if not introduction:
            parts = []
            for key in ("근무형태", "급여", "근무일수"):
                if key in fields:
                    parts.append(f"{key}: {fields[key]}")
            has_recognized_sections = any(
                (body_sections.main_duties, body_sections.requirements, body_sections.preferred)
            )
            if jd_body and not has_recognized_sections:
                parts.append(jd_body)
            introduction = "\n".join(parts)
        main_duties = _saramin_section_text(
            _merge_saramin_section_items(body_sections.main_duties, detail_sections.main_duties)
        )
        requirements = _saramin_section_text(
            _merge_saramin_section_items(body_sections.requirements, detail_sections.requirements)
        )
        preferred = _saramin_section_text(
            _merge_saramin_section_items(body_sections.preferred, detail_sections.preferred)
        )
        benefits_parts = []
        for key in ("급여제도", "선물", "교육/생활", "근무 환경", "조직문화", "리프레시"):
            if key in fields:
                benefits_parts.append(f"{key}: {fields[key]}")
        hiring_process = fields.get("전형절차", "")
        if hiring_process:
            benefits_parts.append(f"전형절차: {hiring_process}")
        return (
            company,
            position,
            _format_jd_markdown(
                title=position,
                company=company,
                experience=experience,
                location=location,
                url=url,
                introduction=introduction,
                main_duties=main_duties,
                requirements=requirements,
                preferred=preferred,
                benefits="\n".join(benefits_parts),
                source="Saramin",
            ),
            CompanyEnrichmentContext(
                platform="saramin",
                item_id=f"saramin:{job_id}",
                company_name=company,
                company_id=csn,
                source_url=detail_url,
                facts={},
                fact_sources={},
            ),
        )


_CLOSED_MARKERS = (
    "채용 마감",
    "채용이 마감",
    "마감되었습니다",
    "이 공고는 마감",
    "지원 기간이 종료",
    "상시채용 종료",
    "Position closed",
    "이 포지션은 마감",
)
_PRIOR_APPLICATION_STATUSES = {
    ApplicationStatus.APPLIED,
    ApplicationStatus.REJECTED,
    ApplicationStatus.INTERVIEW,
    ApplicationStatus.OFFER,
}


def _pre_screen_reason(
    record: StoredJobRecord,
    prior_records: Sequence[StoredJobRecord],
    quick_filters: Mapping[str, Any],
) -> str | None:
    if any(marker in record.jd_markdown for marker in _CLOSED_MARKERS):
        return "closed"
    company_slug = slugify_company(record.record.company, max_len=30, fallback="")
    if len(company_slug) >= 2:
        cutoff = (datetime.now() - timedelta(days=180)).timestamp()
        for prior in prior_records:
            if prior.record.key == record.record.key:
                continue
            if prior.record.application_status not in _PRIOR_APPLICATION_STATUSES:
                continue
            if not prior.record.application_status_updated_at:
                continue
            try:
                updated_at = datetime.fromisoformat(prior.record.application_status_updated_at).timestamp()
            except ValueError:
                continue
            if updated_at < cutoff:
                continue
            prior_slug = slugify_company(prior.record.company, max_len=30, fallback="")
            if company_slug == prior_slug:
                return "prior_application"
            if len(company_slug) >= 4 and len(prior_slug) >= 4 and (
                company_slug in prior_slug or prior_slug in company_slug
            ):
                return "prior_application"
    position = record.record.position
    # The conjunct sits inside each inferring branch, not above the chain: the returns
    # further up rest on evidence (a closed marker, a recorded application) rather than
    # on a guess about the role, so requirements must not cancel them.
    confirmed: bool | None = None

    def backend_confirmed() -> bool:
        nonlocal confirmed
        if confirmed is None:
            confirmed = requirements_show_backend(record.jd_markdown)
        return confirmed

    if quick_filter_title(position, {"quick_filters": quick_filters}) == "pass":
        # A seniority keyword is evidence like a closed marker, not a guess about the
        # role: backend work in the body does not make a 신입 posting a senior one.
        # Role and domain keywords are a guess, and the requirements outrank them.
        excluded_by = matched_title_exclusion(position, {"quick_filters": quick_filters})
        if (excluded_by is not None and is_level_exclusion(excluded_by)) or not backend_confirmed():
            return "title_exclude"
    domain = classify_non_backend_domain(position)
    if domain and not has_domain_counter_indicator(position, domain) and not backend_confirmed():
        return f"domain_{domain}"
    return None


class JobsScreeningStage:
    def __init__(
        self,
        *,
        workspace: WorkspacePaths,
        repository: JDRecordRepository,
        llm_provider: LLMProvider | None = None,
        candidate_context: str | None = None,
        quick_filters: Mapping[str, Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.repository = repository
        self.llm_provider = llm_provider
        self.candidate_context = candidate_context
        self.quick_filters = dict(quick_filters) if quick_filters is not None else None

    def screen(
        self,
        extraction: ExtractionBatch,
        *,
        dry_run: bool,
        llm_timeout: int,
        local_llm_timeout: int | None = None,
    ) -> ScreeningBatch:
        item_ids: list[str] = []
        verdict_counts: Counter[str] = Counter()
        providers: Counter[str] = Counter()
        prescreen_reasons: Counter[str] = Counter()
        failures: list[dict[str, str]] = []
        fallback_count = 0
        # Per status, not last-write-wins: a provider that times out on one record
        # and succeeds on the next would otherwise report only the success, hiding
        # exactly the failure this telemetry exists to surface.
        provider_attempts: dict[str, Counter[str]] = defaultdict(Counter)
        items: list[dict[str, object]] = []
        evidence_violations: Counter[str] = Counter()
        context_warning_messages: list[str] = []
        downgraded = 0
        capped = 0
        company_info = CompanyInfoService(workspace=self.workspace)
        enrichment = CompanyEnrichmentService(company_info=company_info)
        company_info_warnings: dict[str, str] = {}
        company_info_results: dict[str, dict[str, object | None]] = {}
        prior_records = self.repository.list()
        quick_filters = (
            self.quick_filters
            if self.quick_filters is not None
            else _load_quick_filters(self.workspace)
        )
        screening_only = extraction.metadata.get("mode") == "screening_only"
        for record in extraction.records:
            logger.debug("screening: %s:%s", record.record.platform, record.record.job_id)
            item_id = f"{record.record.platform}:{record.record.job_id}"
            context = extraction.company_contexts.get(item_id)
            try:
                company_result, company_error, effective_company_file = _resolve_company_screening_state(
                    enrichment=enrichment,
                    company_info=company_info,
                    company_name=record.record.company,
                    context=context,
                    context_attempted=screening_only,
                    dry_run=dry_run,
                )
            except (FileNotFoundError, OSError, RuntimeError, TimeoutError, ValueError):
                logger.warning("company info failed: %s", _COMPANY_INFO_FAILURE_CODE)
                failures.append(_public_company_info_failure(item_id))
                continue
            company_info_results[item_id] = _sanitize_company_info_result(company_result)
            if company_result.status == "error":
                logger.warning("company info invalid: %s", _COMPANY_INFO_FAILURE_CODE)
                failures.append(_public_company_info_failure(item_id))
                continue
            if company_error is not None:
                company_info_warnings[item_id] = company_error
            prescreen_reason = None
            if not screening_only:
                prescreen_reason = _pre_screen_reason(
                    record,
                    prior_records,
                    quick_filters,
                )
            if prescreen_reason is not None:
                prescreen_reasons[prescreen_reason] += 1
                if not dry_run:
                    if prescreen_reason == "closed":
                        self.repository.update_status(record.record.key, posting_status=PostingStatus.CLOSED)
                    elif prescreen_reason == "prior_application":
                        self.repository.update_status(record.record.key, application_status=ApplicationStatus.REJECTED)
                    self.repository.update_prescreen(record.record.key, prescreen_reason)
                continue
            try:
                result = run_screening(
                    workspace=self.workspace,
                    jd=record,
                    company_file=effective_company_file,
                    llm_timeout=llm_timeout,
                    local_llm_timeout=local_llm_timeout,
                    dry_run=dry_run,
                    llm_provider=self.llm_provider,
                    repository=None if dry_run else self.repository,
                    candidate_context=self.candidate_context,
                )
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                failures.append({"job_key": item_id, "error": str(exc)})
                continue
            item_ids.append(item_id)
            verdict_counts[result.verdict] += 1
            providers[result.provider] += 1
            if result.used_fallback:
                fallback_count += 1
            item_provider_attempts = _sanitize_provider_attempts(
                getattr(result, "provider_attempts", {})
            )
            for attempt_label, attempt_details in item_provider_attempts.items():
                for attempt_detail in attempt_details:
                    provider_attempts[attempt_label][attempt_detail] += 1
            evidence_violations.update(result.evidence_violations)
            verdict_capped = bool(getattr(result, "verdict_capped", False))
            downgraded_flag = bool(getattr(result, "downgraded", False))
            published = bool(getattr(result, "published", False))
            fallback_reason = _sanitize_telemetry_detail(
                getattr(result, "fallback_reason", None)
            )
            context_warning = _sanitize_telemetry_detail(
                getattr(result, "context_warning", None)
            )
            downgraded += int(downgraded_flag)
            capped += int(verdict_capped)
            if context_warning is not None:
                context_warning_messages.append(context_warning)
            items.append(
                {
                    "job_key": item_id,
                    "provider": result.provider,
                    "verdict": result.verdict,
                    "verdict_capped": verdict_capped,
                    "downgraded": downgraded_flag,
                    "published": published,
                    "used_fallback": result.used_fallback,
                    "fallback_reason": fallback_reason,
                    "provider_attempts": item_provider_attempts,
                    "context_warning": context_warning,
                }
            )
        metadata = {
            "item_ids": item_ids,
            "verdict_counts": dict(sorted(verdict_counts.items())),
            "providers": dict(sorted(providers.items())),
            "provider_attempts": {
                label: dict(sorted(counts.items()))
                for label, counts in sorted(provider_attempts.items())
            },
            "items": items,
            "fallback_count": fallback_count,
            "downgraded": downgraded,
            "capped": capped,
            "evidence_violations": dict(sorted(evidence_violations.items())),
            "context_warnings": len(context_warning_messages),
            # The message carries the measured token count; a bare count cannot
            # tell you how close to the window the run actually got.
            "context_warning_messages": sorted(set(context_warning_messages)),
            "failure_count": len(failures),
            "failures": failures,
            "company_info_results": company_info_results,
            "company_info_warnings": company_info_warnings,
            "prescreened_count": sum(prescreen_reasons.values()),
            "prescreen_reasons": dict(sorted(prescreen_reasons.items())),
        }
        return ScreeningBatch(item_ids=tuple(item_ids), metadata=metadata)


def _resolve_company_screening_state(
    *,
    enrichment: CompanyEnrichmentService,
    company_info: CompanyInfoService,
    company_name: str,
    context: CompanyEnrichmentContext | None,
    context_attempted: bool,
    dry_run: bool,
) -> tuple[CompanyInfoEnrichmentResult, str | None, Path | None]:
    lookup = company_info.inspect(company_name)
    if lookup.status == "ready":
        result = CompanyInfoEnrichmentResult(
            status="ready",
            attempted=False,
            persisted=False,
            completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
            warning_code=None,
            file_path=lookup.file_path,
        )
        return result, None, result.file_path
    if context is not None:
        context = _enrichment_context_with_fetched_facts(context)
        result = enrichment.enrich(context, dry_run=dry_run)
    else:
        if lookup.status == "missing":
            result = CompanyInfoEnrichmentResult(
                status="warning",
                attempted=context_attempted,
                persisted=False,
                completeness=None,
                warning_code="missing",
                file_path=None,
            )
        elif lookup.status == "incomplete":
            result = CompanyInfoEnrichmentResult(
                status="warning",
                attempted=context_attempted,
                persisted=False,
                completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
                warning_code="below_threshold",
                file_path=lookup.file_path,
            )
        else:
            result = CompanyInfoEnrichmentResult(
                status="error",
                attempted=False,
                persisted=False,
                completeness=lookup.validation.completeness_score if lookup.validation is not None else None,
                warning_code=None,
                file_path=lookup.file_path,
            )
    company_error = _company_info_warning_from_result(result)
    effective_company_file = None if company_error == _COMPANY_INFO_MISSING else result.file_path
    return result, company_error, effective_company_file


def _company_info_warning_from_result(
    result: CompanyInfoEnrichmentResult,
) -> str | None:
    if result.status != "warning":
        return None
    if result.warning_code == "missing":
        return _COMPANY_INFO_MISSING
    return _COMPANY_INFO_INCOMPLETE


def _public_company_info_failure(item_id: str) -> dict[str, str]:
    return {
        "job_key": item_id,
        "error_code": _COMPANY_INFO_FAILURE_CODE,
        "error": _COMPANY_INFO_FAILURE_MESSAGE,
    }


def _sanitize_company_info_result(
    result: CompanyInfoEnrichmentResult,
) -> dict[str, object | None]:
    return {
        "status": result.status,
        "attempted": result.attempted,
        "persisted": result.persisted,
        "completeness": result.completeness,
        "warning_code": result.warning_code,
    }


def _enrichment_context_with_fetched_facts(
    context: CompanyEnrichmentContext,
) -> CompanyEnrichmentContext:
    if context.platform == "groupby":
        return _groupby_context_with_corroborated_wanted_facts(context)
    if context.facts or not context.company_id:
        return context
    if context.platform == "remember":
        info = remember_company_http(context.company_id)
        source_url = f"https://career.rememberapp.co.kr/job/company/{info.company_id}"
        facts = {
            "industry": info.industry,
            "founded_year": _safe_year(info.established),
            "employee_current": info.employee_count,
            "employee_joined_1y": sum(_safe_int(stat.get("join")) or 0 for stat in info.employee_stats) or None,
            "employee_left_1y": sum(_safe_int(stat.get("leave")) or 0 for stat in info.employee_stats) or None,
        }
        return replace(
            context,
            company_id=str(info.company_id),
            source_url=source_url,
            facts={key: value for key, value in facts.items() if value not in (None, "", ())},
            fact_sources={
                key: (source_url,)
                for key, value in facts.items()
                if value not in (None, "", ())
            },
        )
    if context.platform == "saramin":
        info = saramin_company_http(context.company_id)
        source_url = f"https://m.saramin.co.kr/job-search/company-info-view?csn={context.company_id}"
        facts = {
            "industry": info.industry,
            "founded_year": _safe_year(info.founded_date),
            "employee_current": info.employee_count,
        }
        return replace(
            context,
            source_url=source_url,
            facts={key: value for key, value in facts.items() if value not in (None, "", ())},
            fact_sources={
                key: (source_url,)
                for key, value in facts.items()
                if value not in (None, "", ())
            },
        )
    if context.platform == "wanted":
        try:
            info = wanted_company_http(context.company_id)
        except (ValueError, OSError, RuntimeError, TypeError, AttributeError, KeyError):
            return context
        if not wanted_company_is_valid(info):
            return context
        facts, fact_sources, source_url = _wanted_fact_payload(info)
        return replace(
            context,
            source_url=source_url,
            facts=facts,
            fact_sources=fact_sources,
        )
    return context


def _groupby_context_with_corroborated_wanted_facts(
    context: CompanyEnrichmentContext,
) -> CompanyEnrichmentContext:
    sanitized = replace(context, company_id=None)
    try:
        wanted_company_id = wanted_search_company_id(
            context.company_name,
            verify_industry=str(context.facts.get("industry", "") or ""),
            verify_location=str(context.facts.get("location", "") or ""),
        )
    except (ValueError, OSError, RuntimeError, TypeError, AttributeError, KeyError):
        return sanitized
    if wanted_company_id is None:
        return sanitized
    try:
        info = wanted_company_http(wanted_company_id)
    except (ValueError, OSError, RuntimeError, TypeError, AttributeError, KeyError):
        return sanitized
    if int(info.company_id) != int(wanted_company_id):
        return sanitized
    if not wanted_company_matches(
        info,
        context.company_name,
        verify_industry=str(context.facts.get("industry", "") or ""),
        verify_location=str(context.facts.get("location", "") or ""),
    ):
        return sanitized
    wanted_facts, wanted_sources, _wanted_source_url = _wanted_fact_payload(info)
    merged_facts = dict(context.facts)
    merged_sources = dict(context.fact_sources)
    for key, value in wanted_facts.items():
        if key in merged_facts and merged_facts[key] not in (None, "", ()):
            continue
        merged_facts[key] = value
        merged_sources[key] = wanted_sources[key]
    return replace(
        sanitized,
        facts=merged_facts,
        fact_sources=merged_sources,
    )


def _wanted_fact_payload(
    info: WantedCompanyInfo,
) -> tuple[dict[str, object], dict[str, tuple[str, ...]], str]:
    source_url = f"https://www.wanted.co.kr/company/{info.company_id}"
    facts: dict[str, object] = {
        "industry": info.industry,
        "founded_year": info.founded_year,
        "employee_current": info.employee_count,
        "avg_salary": info.avg_salary_manwon,
        "employee_joined_1y": info.hired_1y,
        "employee_left_1y": info.left_1y,
        "revenue": info.total_sales_eok,
    }
    allowlisted = {
        key: value for key, value in facts.items() if value not in (None, "", ())
    }
    return (
        allowlisted,
        {
            key: (source_url,)
            for key in allowlisted
        },
        source_url,
    )


def _load_quick_filters(workspace: WorkspacePaths) -> dict[str, Any]:
    config_path = workspace.jobs_config_dir / "search_config.yaml"
    if not config_path.exists():
        return {}
    raw = YamlConfigFileAdapter(config_path).read()
    quick_filters = raw.get("quick_filters", {})
    return dict(quick_filters) if isinstance(quick_filters, Mapping) else {}


def load_candidate_context(workspace: WorkspacePaths) -> str:
    profile_dir = workspace.private_dir / "profile"
    companies_dir = workspace.private_dir / "companies"
    paths = [profile_dir / name for name in PROFILE_CONTEXT_FILES]
    if companies_dir.exists():
        paths.extend(sorted(companies_dir.glob("*/profile.md")))
        paths.extend(sorted(companies_dir.glob("*/projects/*.md")))

    blocks: list[str] = []
    total_chars = 0
    for path in paths:
        if not path.is_file() or path.name == "CLAUDE.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) > MAX_CANDIDATE_FILE_CHARS:
            text = text[:MAX_CANDIDATE_FILE_CHARS].rstrip() + "\n...(truncated)"
        try:
            label = path.relative_to(workspace.root)
        except ValueError:
            label = path
        block = f"[source: {label}]\n{text}"
        next_total = total_chars + len(block) + 2
        if next_total > MAX_CANDIDATE_CONTEXT_CHARS:
            remaining = MAX_CANDIDATE_CONTEXT_CHARS - total_chars
            if remaining > 200:
                blocks.append(block[:remaining].rstrip() + "\n...(context truncated)")
            break
        blocks.append(block)
        total_chars = next_total
    return "\n\n---\n\n".join(blocks) if blocks else "후보자 이력 파일 없음"


class JobsCompletionStage:
    def __init__(
        self,
        *,
        pipeline: JobsPipelineService,
        maintenance: JobsMaintenanceService,
    ) -> None:
        self.pipeline = pipeline
        self.maintenance = maintenance

    def complete(
        self,
        extraction: ExtractionBatch,
        screening: ScreeningBatch,
        *,
        dry_run: bool,
        no_classify: bool,
    ) -> CompletionBatch:
        results: list[dict[str, Any]] = []
        if no_classify or dry_run:
            reason = "classification disabled" if no_classify else "dry-run preview"
            results = [
                {"job_key": item_id, "outcome": "skipped", "message": reason}
                for item_id in screening.item_ids
            ]
        else:
            for item_id in screening.item_ids:
                result = self.pipeline.classify_record(_parse_job_key(item_id), dry_run=dry_run)
                results.append(_ingest_result_to_dict(item_id, result))
        metadata: dict[str, Any] = {
            "results": results,
            "summary_rebuilt": False,
        }
        if not dry_run:
            summary = self.maintenance.rebuild_summary()
            metadata["summary_rebuilt"] = True
            metadata["summary_output_path"] = str(summary.output_path)
            metadata["summary_record_count"] = summary.record_count
        return CompletionBatch(item_ids=tuple(screening.item_ids), metadata=metadata)


def _build_auto_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="career-jobs run auto", add_help=False)
    parser.add_argument("--from-urls", type=Path)
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument("--screening-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-urls", type=_positive_int)
    parser.add_argument("--llm-timeout", type=_positive_int, default=120)
    parser.add_argument("--local-llm-timeout", type=_positive_int)
    parser.add_argument("--no-classify", action="store_true")
    parser.add_argument("--json", dest="json_mode", action="store_true")
    return parser


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return value


def _read_urls(path: Path) -> tuple[str, ...]:
    if not path.exists():
        raise FileNotFoundError(path)
    return tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _render_json(payload: dict[str, Any], json_mode: bool) -> str:
    if json_mode:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lines: list[str] = []
    for key, value in payload.items():
        if key in {"counts", "diagnostics", "capabilities"}:
            continue
        if key == "extraction" and isinstance(value, Mapping) and "items" in value:
            visible = {k: v for k, v in value.items() if k != "items"}
            lines.append(f"{key}={visible}")
            prefix = "item" if value.get("mode") == "screening_only" else "new"
            for item in value["items"]:
                lines.append(f"{prefix}: {item['company']} — {item['position']} ({item['job_key']})")
        elif key == "screening" and isinstance(value, Mapping) and "items" in value:
            visible = {k: v for k, v in value.items() if k != "items"}
            lines.append(f"{key}={visible}")
        else:
            lines.append(f"{key}={value}")
    counts = payload.get("counts")
    if counts:
        lines.append("counts=" + ",".join(f"{name}:{value}" for name, value in counts.items()))
    diagnostics = payload.get("diagnostics")
    if diagnostics:
        lines.append("diagnostics=" + ",".join(str(item) for item in diagnostics))
    return "\n".join(lines) + "\n"


def _extract_next_data(html: str) -> dict[str, Any]:
    from careerkit.jobs.adapters.platforms._next_data import extract_next_data

    return extract_next_data(html)


def _normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _safe_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _safe_year(value: object) -> int | None:
    text = _normalize_text(str(value or ""))
    match = re.search(r"(\d{4})", text)
    return int(match.group(1)) if match else None


def _first_groupby_value(
    name: str,
    startup: Mapping[str, Any],
    position: Mapping[str, Any],
) -> Any:
    value = startup.get(name)
    if value not in (None, "", []):
        return value
    return position.get(name)


def _format_groupby_company_context(
    startup: Mapping[str, Any],
    position: Mapping[str, Any],
) -> str:
    def value(name: str) -> Any:
        return startup.get(name) if startup.get(name) not in (None, "", []) else position.get(name)

    lines: list[str] = []
    intro = _html_to_text(value("briefIntro") or "")
    if intro:
        lines.append(f"회사 소개: {intro}")
    for field, label in (("memberCount", "전체 인원"), ("devCount", "개발 인원")):
        count = value(field)
        if count not in (None, ""):
            lines.append(f"{label}: {count}명")
    funding_round = _normalize_text(str(value("fundingRound") or ""))
    if funding_round:
        lines.append(f"투자 단계: {funding_round}")
    service_areas = value("serviceAreas")
    if isinstance(service_areas, list):
        names = []
        for item in service_areas:
            raw = item.get("name", "") if isinstance(item, Mapping) else item
            name = _normalize_text(str(raw or ""))
            if name:
                names.append(name)
        if names:
            lines.append(f"서비스 분야: {', '.join(names)}")
    return "\n".join(lines)


def _normalize_canonical_bullets(text: str) -> str:
    return re.sub(r"(?m)^(?P<indent>\s*)•\s*(?P<text>.*\S)\s*$", r"\g<indent>- \g<text>", text)


def _format_jd_markdown(
    *,
    title: str,
    company: str,
    experience: str,
    location: str,
    url: str,
    introduction: str,
    main_duties: str,
    requirements: str,
    preferred: str,
    benefits: str,
    source: str,
) -> str:
    return f"""# {title}

## 기본 정보

| 항목 | 내용 |
|------|------|
| 회사명 | {company} |
| 포지션 | {title} |
| 경력 | {experience or '정보 없음'} |
| 근무지 | {location or '정보 없음'} |
| 출처 | [{source}]({url}) |

## 포지션 소개

{introduction or '정보 없음'}

## 주요 업무

{_normalize_canonical_bullets(main_duties) or '정보 없음'}

## 자격 요건

{_normalize_canonical_bullets(requirements) or '정보 없음'}

## 우대사항

{_normalize_canonical_bullets(preferred) or '정보 없음'}

## 혜택 및 복지

{benefits or '정보 없음'}
"""


def _saramin_cross_source_key(text: str) -> str:
    return _normalize_text(text.removeprefix("- "))


def _merge_saramin_section_items(body_items: Sequence[str], detail_items: Sequence[str]) -> tuple[str, ...]:
    merged = list(body_items)
    seen_body = {_saramin_cross_source_key(item) for item in body_items}
    for item in detail_items:
        if _saramin_cross_source_key(item) not in seen_body:
            merged.append(item)
    return tuple(merged)


def _saramin_section_text(items: Sequence[str]) -> str:
    return "\n".join(item for item in items if _normalize_text(item))


def _merge_saramin_intro(items: Sequence[str]) -> str:
    return "\n".join(item for item in items if _normalize_text(item))


def _format_wanted_experience(job: dict[str, Any]) -> str:
    career = job.get("career") or {}
    min_exp = career.get("annual_from")
    max_exp = career.get("annual_to")
    if min_exp == 0 and max_exp == 0:
        return "신입"
    if min_exp and max_exp:
        return f"{min_exp}~{max_exp}년"
    if min_exp:
        return f"{min_exp}년 이상"
    return "경력"


def _format_remember_experience(posting: dict[str, Any]) -> str:
    min_exp = posting.get("minExperience")
    max_exp = posting.get("maxExperience")
    if min_exp and max_exp:
        return f"{min_exp}~{max_exp}년"
    if min_exp:
        return f"{min_exp}년 이상"
    return "경력"


def _format_remember_salary(posting: dict[str, Any]) -> str:
    min_salary = posting.get("minSalary")
    max_salary = posting.get("maxSalary")
    if min_salary and max_salary:
        return f"{min_salary}~{max_salary}만원"
    if min_salary:
        return f"{min_salary}만원 이상"
    if max_salary:
        return f"~{max_salary}만원"
    return ""


def _remember_operator_details(posting: dict[str, Any]) -> list[str]:
    details: list[str] = []
    salary = _format_remember_salary(posting)
    if salary:
        details.append(f"연봉: {salary}")
    rank = _normalize_text(posting.get("jobRankCategory", ""))
    if rank:
        details.append(f"직급: {rank}")
    if posting.get("leaderPosition"):
        details.append("리더 포지션: 예")
    skills = (posting.get("desiredProfileCondition") or {}).get("skills") or []
    skill_names = [
        _normalize_text(item.get("name", ""))
        for item in skills
        if isinstance(item, dict) and _normalize_text(item.get("name", ""))
    ]
    if skill_names:
        details.append(f"기술스택: {', '.join(skill_names)}")
    for label, key in (("기업정보", "chips"), ("복지", "classifiedTags")):
        items = posting.get(key) or []
        values = [
            _normalize_text(item.get("value", ""))
            for item in items
            if isinstance(item, dict) and _normalize_text(item.get("value", ""))
        ]
        if values:
            details.append(f"{label}: {', '.join(values)}")
    process = str(posting.get("recruitingProcess", "") or "").strip()
    if process:
        details.append(f"채용 절차:\n{process}")
    additional = str(posting.get("additionalInformation", "") or "").strip()
    if additional:
        details.append(additional)
    return details


def _format_remember_address(posting: dict[str, Any]) -> str:
    addresses = posting.get("addresses")
    if not isinstance(addresses, list):
        return ""
    parts = []
    for address in addresses:
        if not isinstance(address, dict):
            continue
        text = _normalize_text(
            f"{address.get('addressLevel1', '')} {address.get('addressLevel2', '')}"
        )
        if text:
            parts.append(text)
    return ", ".join(parts)


def _html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = html
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<p[^>]*>", "", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _job_key_from_url(url: str) -> JobKey | None:
    job_id = extract_job_id(url)
    platform = get_platform_from_url(url)
    if not job_id or not platform:
        return None
    return JobKey(platform, job_id)


def _save_or_preview_record(
    *,
    repository: JDRecordRepository,
    key: JobKey,
    company: str,
    position: str,
    source_url: str,
    jd_markdown: str,
    dry_run: bool,
) -> StoredJobRecord:
    existing = repository.find(key)
    if existing is not None:
        record = replace(
            existing.record,
            company=company,
            position=position,
            source_url=source_url,
        )
        screening_markdown = existing.screening_markdown
    else:
        record = JobRecord(
            platform=key.platform,
            job_id=key.job_id,
            company=company,
            position=position,
            source_url=source_url,
        )
        screening_markdown = None
    normalized_markdown = jd_markdown.rstrip() + "\n"
    if dry_run:
        return StoredJobRecord(
            record=record,
            jd_markdown=normalized_markdown,
            screening_markdown=screening_markdown,
        )
    return repository.save(record, jd_markdown=normalized_markdown)


def _parse_job_key(raw: str) -> JobKey:
    if ":" not in raw:
        raise ValueError(f"job_key must be platform:job_id, got {raw!r}")
    platform, job_id = raw.split(":", 1)
    return JobKey(platform, job_id)


def _ingest_result_to_dict(item_id: str, result: IngestResult) -> dict[str, Any]:
    return {
        "job_key": item_id,
        "outcome": result.outcome,
        "message": result.message,
        "target": result.target,
        "current": result.current,
        "verdict": result.verdict,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    _atomic_write_text(path, serialized)


def _sanitize_provider_attempts(raw: object) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        return {}
    sanitized: dict[str, list[str]] = {}
    for label, details in raw.items():
        label_text = str(label).strip()
        if not label_text:
            continue
        if not isinstance(details, Sequence) or isinstance(details, str):
            details = (details,)
        item_details = [
            cleaned
            for detail in details
            if (cleaned := _sanitize_telemetry_detail(detail)) is not None
        ]
        if item_details:
            sanitized[label_text] = item_details
    return dict(sorted(sanitized.items()))


def _sanitize_telemetry_detail(detail: object) -> str | None:
    if detail is None:
        return None
    text = str(detail).replace("\r", "\n").strip()
    if not text:
        return None
    text = re.sub(r"\?[^ \n\)\]]+", "", text)
    text = re.sub(
        r"(\b[A-Za-z0-9_-]*(?:secret|token|api[_-]?key|access[_-]?key)[A-Za-z0-9_-]*\s*=\s*)[^\s]+",
        r"\1[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:(?<=^)|(?<=[\s=:\(\[]))/[^\s\)\]]+", "[path]", text)
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return None
    if len(first_line) > MAX_TELEMETRY_DETAIL_CHARS:
        first_line = first_line[:MAX_TELEMETRY_DETAIL_CHARS].rstrip() + "..."
    return first_line


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _has_assessable_requirements(jd_markdown: str) -> bool:
    from careerkit.jobs.application.requirement_manifest import (
        extract_requirement_manifest,
        without_main_duty,
    )

    try:
        manifest = extract_requirement_manifest(jd_markdown)
        filtered = without_main_duty(manifest)
        return any(item.assessable for item in filtered.leaves)
    except (ValueError, AttributeError, TypeError, IndexError):
        return False
