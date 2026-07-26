"""Flag-day migration from legacy JD folders to canonical file records.

The migration is deliberately split into inventory, staging, validation, and
activation.  Preflight never mutates legacy inputs or the active root.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable

from careerkit.jobs.application.status import normalize_status, parse_frontmatter
from careerkit.jobs.domain.verdict import parse_verdict_from_screening, to_screening_verdict
from careerkit.jobs.domain.model import (
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    SCHEMA_VERSION,
    ScreeningVerdict,
)
from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    canonical_records_root,
)


_URL_RE = re.compile(r"https?://[^\s)\]>|]+")
_KNOWN_PLATFORMS = {
    "wanted", "remember", "saramin", "jobkorea", "jumpit", "groupby",
    "offercent", "greeting", "private", "headhunter",
}
_ID_LIST_KEYS = {"seen_job_ids", "processed_ids", "job_ids"}


def extract_metadata_from_jd(jd_content: str) -> dict[str, str]:
    frontmatter = parse_frontmatter(jd_content)
    aliases = {
        "company": ("company", "company_name", "회사", "회사명"),
        "position": ("position", "title", "포지션", "직무"),
        "experience": ("experience", "경력"),
        "location": ("location", "근무지", "근무지역"),
        "employment_type": ("employment_type", "employment", "고용형태"),
    }
    metadata = {
        field: frontmatter[key].strip()
        for field, keys in aliases.items()
        for key in keys
        if frontmatter.get(key, "").strip()
    }
    patterns = {
        "company": r"\|\s*회사명\s*\|\s*([^|]+)\|",
        "position": r"\|\s*포지션\s*\|\s*([^|]+)\|",
        "experience": r"\|\s*경력\s*\|\s*([^|]+)\|",
        "location": r"\|\s*근무지역?\s*\|\s*([^|]+)\|",
        "employment_type": r"\|\s*고용형태\s*\|\s*([^|]+)\|",
    }
    for key, pattern in patterns.items():
        if key in metadata:
            continue
        match = re.search(pattern, jd_content)
        if match:
            metadata[key] = match.group(1).strip()
    bullet_pattern = re.compile(
        r"(?m)^-\s*(?:\*\*)?"
        r"(?P<label>회사명?|포지션|직무|경력|근무지역?|고용형태)"
        r"(?:\*\*)?\s*:\s*(?P<value>.+?)\s*$"
    )
    bullet_fields = {
        "회사": "company",
        "회사명": "company",
        "포지션": "position",
        "직무": "position",
        "경력": "experience",
        "근무지": "location",
        "근무지역": "location",
        "고용형태": "employment_type",
    }
    for match in bullet_pattern.finditer(jd_content):
        metadata.setdefault(
            bullet_fields[match.group("label")],
            match.group("value").strip(),
        )
    url_match = re.search(r"출처:\s*\[.*?\]\((https?://[^\)]+)\)", jd_content)
    if url_match:
        metadata["url"] = url_match.group(1)
    return metadata


def extract_job_id(url: str) -> str | None:
    gb_match = re.search(r"groupby\.kr/positions/(\d+)", url)
    if gb_match:
        return gb_match.group(1)
    patterns = [
        r"wanted\.co\.kr/wd/(\d+)",
        r"rememberapp\.co\.kr/job/(?:posting/)?(\d+)",
        r"career\.rememberapp\.co\.kr/job/posting/(\d+)",
        r"saramin\.co\.kr.*rec_idx=(\d+)",
        r"jobkorea\.co\.kr/Recruit/GI_Read/(\d+)",
        r"jumpit\.saramin\.co\.kr/position/(\d+)",
        r"offercent\.co\.kr/jd/(\d+)",
        r"career\.greetinghr\.com/(?:[a-z]{2}/)?o/(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_job_id_from_filename(filename: str) -> str | None:
    platform_prefixes = {"groupby"}
    stem = Path(filename).stem if "." in filename else filename
    parts = stem.split("-")
    if not parts:
        return None
    if parts[0].isdigit():
        return parts[0]
    if parts[0] in platform_prefixes and len(parts) > 1 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}"
    if len(parts) > 1 and parts[1].isdigit():
        return parts[1]
    return parts[0]


def get_platform_from_url(url: str) -> str | None:
    if "wanted.co.kr" in url:
        return "wanted"
    if "rememberapp.co.kr" in url:
        return "remember"
    if "jumpit.saramin.co.kr" in url:
        return "jumpit"
    if "saramin.co.kr" in url:
        return "saramin"
    if "jobkorea.co.kr" in url:
        return "jobkorea"
    if "groupby.kr" in url:
        return "groupby"
    if "offercent.co.kr" in url:
        return "offercent"
    if "career.greetinghr.com" in url:
        return "greeting"
    return None


@dataclass(frozen=True)
class MigrationPaths:
    legacy_private: Path
    stage_root: Path
    active_root: Path
    report_path: Path


@dataclass(frozen=True, order=True)
class MigrationFinding:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class MigrationMapping:
    source: str | None
    platform: str
    job_id: str
    screening_source: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source": self.source,
            "platform": self.platform,
            "job_id": self.job_id,
            "screening_source": self.screening_source,
        }


@dataclass(frozen=True)
class MigrationReport:
    ready: bool
    activated: bool
    jd_count: int
    screening_count: int
    staged_record_count: int
    staged_screening_count: int
    mappings: tuple[MigrationMapping, ...]
    source_hashes: dict[str, str]
    blockers: tuple[MigrationFinding, ...] = ()
    notices: tuple[MigrationFinding, ...] = ()
    ignored: tuple[str, ...] = ()
    status_distribution: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ready": self.ready,
            "activated": self.activated,
            "counts": {
                "legacy_jds": self.jd_count,
                "legacy_screenings": self.screening_count,
                "staged_records": self.staged_record_count,
                "staged_screenings": self.staged_screening_count,
            },
            "mappings": [item.to_dict() for item in self.mappings],
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "status_distribution": dict(sorted((self.status_distribution or {}).items())),
            "blockers": [item.to_dict() for item in self.blockers],
            "notices": [item.to_dict() for item in self.notices],
            "ignored": list(self.ignored),
        }


@dataclass(frozen=True)
class _LegacyJD:
    path: Path | None
    relative: str | None
    record: JobRecord
    content: str


class StorageMigrator:
    def __init__(self, paths: MigrationPaths) -> None:
        self.paths = paths
        self._validate_paths()

    def _validate_paths(self) -> None:
        legacy = self.paths.legacy_private.resolve(strict=False)
        stage = self.paths.stage_root.resolve(strict=False)
        active = self.paths.active_root.resolve(strict=False)
        report = self.paths.report_path.resolve(strict=False)

        if stage == legacy or stage in legacy.parents or legacy in stage.parents:
            raise ValueError("stage_root must not overlap legacy_private")
        if stage == active or stage in active.parents or active in stage.parents:
            raise ValueError("stage_root must not overlap active_root")
        if stage == report or stage in report.parents:
            raise ValueError("report_path must not be inside stage_root")

    def inventory(self) -> tuple[list[Path], list[Path], list[Path]]:
        """Return deterministic JD, screening, and owned config/runtime inputs."""
        postings = self.paths.legacy_private / "job_postings"
        screening_root = self.paths.legacy_private / "jd_analysis" / "screening"
        jds = sorted(
            path
            for path in postings.rglob("*.md")
            if path.name not in {"jd-screening-rules.md", "CLAUDE.md"}
            and "auto_results" not in path.parts
        ) if postings.exists() else []
        screenings = sorted(
            path
            for path in screening_root.rglob("*.md")
            if path.name not in {"SUMMARY.md", "PRIORITY.md"}
            and not {"invalid", ".dedup_trash"}.intersection(
                path.relative_to(screening_root).parts
            )
        ) if screening_root.exists() else []
        owned: list[Path] = []
        for relative in (
            "job_postings/search_config.yaml",
            "job_postings/jd-screening-rules.md",
            "job_postings/queue.json",
            "job_postings/.search_state.json",
        ):
            candidate = self.paths.legacy_private / relative
            if candidate.is_file():
                owned.append(candidate)
        for pattern in ("job_postings/.auto_state_*.json", "job_postings/auto_results/*.json"):
            owned.extend(sorted(self.paths.legacy_private.glob(pattern)))
        for pattern in (
            "job_postings/unprocessed/company_enrichment_thevc.txt",
            "job_postings/unprocessed/company_enrichment_saramin.txt",
            "job_postings/unprocessed/search_*.txt",
        ):
            owned.extend(sorted(self.paths.legacy_private.glob(pattern)))
        return jds, screenings, sorted(set(owned))

    def preflight(self) -> MigrationReport:
        """Build a fresh stage and validate it without publishing active data."""
        self._reset_stage()
        jds, screenings, owned = self.inventory()
        ignored_paths = self._ignored_inputs()
        blockers: list[MigrationFinding] = []
        notices: list[MigrationFinding] = []
        source_hashes: dict[str, str] = {}
        for path in (*jds, *screenings, *owned, *ignored_paths):
            relative = self._relative(path)
            try:
                source_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                blockers.append(MigrationFinding("source_read_failed", relative, str(exc)))
        parsed: list[_LegacyJD] = []
        for path in jds:
            candidate, finding = self._parse_jd(path)
            if finding is not None:
                blockers.append(finding)
                continue
            assert candidate is not None
            parsed.append(candidate)

        parsed, duplicate_findings, duplicate_notices = self._resolve_duplicate_jds(parsed)
        blockers.extend(duplicate_findings)
        notices.extend(duplicate_notices)

        pairs, synthetic, pair_findings, pair_notices = self._pair_screenings(parsed, screenings)
        parsed.extend(synthetic)
        blockers.extend(pair_findings)
        notices.extend(pair_notices)
        repository = JDRecordRepository(canonical_records_root(self.paths.stage_root))
        mappings: list[MigrationMapping] = []
        if not blockers:
            for item in sorted(parsed, key=lambda value: value.record.key):
                try:
                    repository.create(item.record, jd_markdown=item.content)
                    screening = pairs.get(item.record.key)
                    if screening is not None:
                        screening_markdown = screening.read_text(encoding="utf-8")
                        repository.update_screening_result(
                            item.record.key,
                            screening_markdown=screening_markdown,
                            screening_verdict=to_screening_verdict(
                                parse_verdict_from_screening(screening_markdown) or ""
                            ),
                        )
                    mappings.append(MigrationMapping(
                        source=item.relative,
                        platform=item.record.platform,
                        job_id=item.record.job_id,
                        screening_source=self._relative(screening) if screening else None,
                    ))
                except (OSError, UnicodeError, ValueError) as exc:
                    blockers.append(MigrationFinding(
                        "stage_copy_failed", item.relative or "", str(exc)
                    ))

        identity = self._identity_lookup(parsed)
        if not blockers:
            blockers.extend(self._stage_owned_files(owned, identity))

        staged_root = canonical_records_root(self.paths.stage_root)
        staged = repository.list() if staged_root.exists() else []
        blockers.extend(self._validate_staged_hashes(mappings, source_hashes))
        staged_screenings = sum(item.screening_markdown is not None for item in staged)
        if len(staged) != len(parsed):
            blockers.append(MigrationFinding(
                "jd_count_mismatch", "job_postings",
                f"expected {len(parsed)} canonical records, staged {len(staged)}",
            ))
        if staged_screenings != len(pairs):
            blockers.append(MigrationFinding(
                "screening_count_mismatch", "jd_analysis/screening",
                f"expected {len(pairs)} canonical screenings, staged {staged_screenings}",
            ))

        distribution: dict[str, int] = {}
        for stored in staged:
            record = stored.record
            key = "/".join((
                record.screening_verdict.value if record.screening_verdict else "none",
                record.application_status.value,
                record.posting_status.value,
            ))
            distribution[key] = distribution.get(key, 0) + 1

        ignored = tuple(self._relative(path) for path in ignored_paths)
        for path in ignored_paths:
            relative = self._relative(path)
            if path.name == "SUMMARY.md":
                code = "derived_summary_ignored"
                message = "SUMMARY.md is rebuilt from canonical records"
            elif "invalid" in path.parts:
                code = "quarantined_screening_ignored"
                message = "quarantined invalid screening is not canonical input"
            elif ".dedup_trash" in path.parts:
                code = "superseded_screening_ignored"
                message = "superseded screening backup is not canonical input"
            else:
                code = "control_document_ignored"
                message = "known control document is not a JD or screening record"
            notices.append(MigrationFinding(code, relative, message))
        blockers = sorted({self._safe_finding(item) for item in blockers})
        notices = [self._safe_finding(item) for item in notices]
        report = MigrationReport(
            ready=not blockers,
            activated=False,
            jd_count=len(jds),
            screening_count=len(screenings),
            staged_record_count=len(staged),
            staged_screening_count=staged_screenings,
            mappings=tuple(sorted(mappings, key=lambda item: (item.platform, item.job_id))),
            source_hashes=source_hashes,
            blockers=tuple(blockers),
            notices=tuple(sorted(notices)),
            ignored=ignored,
            status_distribution=distribution,
        )
        self._write_report(report)
        return report

    def activate(self, report: MigrationReport) -> MigrationReport:
        """Atomically publish a validated stage when no active root exists."""
        if not report.ready or report.blockers:
            return report
        if self.paths.active_root.exists():
            finding = MigrationFinding(
                "active_root_exists", "jd", "refusing to replace an existing active root"
            )
            blocked = replace(report, ready=False, blockers=tuple(sorted((*report.blockers, finding))))
            self._write_report(blocked)
            return blocked
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "records": report.staged_record_count,
            "screenings": report.staged_screening_count,
        }
        self._atomic_json(self.paths.stage_root / "schema.json", manifest)
        activated = replace(report, activated=True)
        self._atomic_json(
            self.paths.stage_root / "runtime/migration-report.json",
            activated.to_dict(),
        )
        self.paths.active_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.paths.stage_root, self.paths.active_root)
        self._fsync_dir(self.paths.active_root.parent)
        self._write_report(activated, include_stage=False)
        return activated

    def run(self, *, dry_run: bool = True) -> MigrationReport:
        report = self.preflight()
        return report if dry_run else self.activate(report)

    def _parse_jd(self, path: Path) -> tuple[_LegacyJD | None, MigrationFinding | None]:
        relative = self._relative(path)
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            return None, MigrationFinding("jd_read_failed", relative, str(exc))
        url_identity = self._identity_from_content(content, filename=path.name)
        filename_id = self._normal_job_id(extract_job_id_from_filename(path.name))
        filename_platform = self._filename_platform(path.name)
        if url_identity is None:
            private_identity = self._private_identity(path.name)
            if private_identity is None:
                return None, MigrationFinding(
                    "unknown_platform_or_id", relative, "no supported platform URL found"
                )
            platform, url_id, source_url = private_identity
        else:
            platform, url_id, source_url = url_identity
        if filename_platform and filename_platform != platform:
            return None, MigrationFinding(
                "identity_platform_conflict", relative,
                f"filename platform {filename_platform} disagrees with URL platform {platform}",
            )
        if (
            self._filename_has_identity_hint(path.name)
            and filename_platform != "private"
            and filename_id != url_id
        ):
            return None, MigrationFinding(
                "identity_id_conflict", relative,
                f"filename ID {filename_id} disagrees with URL ID {url_id}",
            )
        metadata = extract_metadata_from_jd(content)
        company = metadata.get("company") or self._heading(content) or "legacy"
        position = metadata.get("position") or "legacy"
        verdict, application, posting, updated = self._axes(path, content)
        record = JobRecord(
            platform=platform,
            job_id=url_id,
            company=company.strip(),
            position=position.strip(),
            source_url=source_url,
            screening_verdict=verdict,
            application_status=application,
            posting_status=posting,
            application_status_updated_at=updated,
            migration_source=relative,
        )
        return _LegacyJD(path, relative, record, content), None

    def _identity_from_content(
        self, content: str, *, filename: str
    ) -> tuple[str, str, str] | None:
        identities: list[tuple[str, str, str]] = []
        for raw_url in _URL_RE.findall(content):
            url = raw_url.rstrip(".,;'")
            platform = get_platform_from_url(url)
            job_id = extract_job_id(url)
            if platform and job_id:
                identities.append((platform, job_id, url))
        unique = {(platform, job_id) for platform, job_id, _ in identities}
        if len(unique) != 1:
            filename_id = self._numeric_filename_id(filename)
            matching = [identity for identity in unique if identity[1] == filename_id]
            if filename_id is None or len(matching) != 1:
                return None
            platform, job_id = matching[0]
        else:
            platform, job_id = next(iter(unique))
        source_url = next(url for candidate_platform, candidate_id, url in identities if (candidate_platform, candidate_id) == (platform, job_id))
        return platform, job_id, source_url

    def _pair_screenings(
        self, jds: list[_LegacyJD], screenings: list[Path]
    ) -> tuple[
        dict[JobKey, Path], list[_LegacyJD], list[MigrationFinding], list[MigrationFinding]
    ]:
        by_name: dict[str, list[_LegacyJD]] = {}
        by_id: dict[str, list[_LegacyJD]] = {}
        for jd in jds:
            if jd.path is not None:
                by_name.setdefault(jd.path.name, []).append(jd)
            by_id.setdefault(jd.record.job_id, []).append(jd)
        pairs: dict[JobKey, Path] = {}
        synthetic: list[_LegacyJD] = []
        findings: list[MigrationFinding] = []
        notices: list[MigrationFinding] = []
        for screening in screenings:
            relative = self._relative(screening)
            candidates = by_name.get(screening.name, [])
            if not candidates:
                raw_id = self._normal_job_id(extract_job_id_from_filename(screening.name))
                candidates = by_id.get(raw_id or "", [])
                platform = self._filename_platform(screening.name)
                if platform:
                    candidates = [item for item in candidates if item.record.platform == platform]
            if len(candidates) != 1:
                if not candidates and screening.name.startswith("headhunter-"):
                    generated = self._screening_only_record(screening)
                    synthetic.append(generated)
                    pairs[generated.record.key] = screening
                    notices.append(MigrationFinding(
                        "screening_only_placeholder_created",
                        relative,
                        f"created {generated.record.platform}:{generated.record.job_id} with an explicit missing-JD placeholder",
                    ))
                    continue
                code = "orphan_screening" if not candidates else "ambiguous_screening"
                findings.append(MigrationFinding(code, relative, f"resolved candidates: {len(candidates)}"))
                continue
            key = candidates[0].record.key
            if key in pairs:
                findings.append(MigrationFinding(
                    "duplicate_screening", relative, f"more than one screening resolves to {key.platform}:{key.job_id}"
                ))
            else:
                pairs[key] = screening
        return pairs, synthetic, findings, notices

    def _resolve_duplicate_jds(
        self, jds: list[_LegacyJD]
    ) -> tuple[list[_LegacyJD], list[MigrationFinding], list[MigrationFinding]]:
        """Select a structured current snapshot without hiding ambiguous duplicates."""
        grouped: dict[JobKey, list[_LegacyJD]] = {}
        for jd in jds:
            grouped.setdefault(jd.record.key, []).append(jd)
        resolved: list[_LegacyJD] = []
        blockers: list[MigrationFinding] = []
        notices: list[MigrationFinding] = []
        for key, candidates in sorted(grouped.items()):
            if len(candidates) == 1:
                resolved.extend(candidates)
                continue
            structured = [item for item in candidates if self._is_structured_snapshot(item.content)]
            if len(structured) != 1:
                blockers.append(MigrationFinding(
                    "duplicate_composite_key",
                    ",".join(sorted(item.relative or "" for item in candidates)),
                    f"multiple legacy JDs resolve to {key.platform}:{key.job_id}",
                ))
                continue
            selected = structured[0]
            resolved.append(selected)
            for superseded in candidates:
                if superseded is selected:
                    continue
                notices.append(MigrationFinding(
                    "superseded_duplicate_jd",
                    superseded.relative or "",
                    f"superseded by {selected.relative} for {key.platform}:{key.job_id}; both source hashes are retained",
                ))
        return resolved, blockers, notices

    def _is_structured_snapshot(self, content: str) -> bool:
        frontmatter = parse_frontmatter(content)
        return all(str(frontmatter.get(key, "")).strip() for key in ("job_id", "source", "url"))

    def _screening_only_record(self, screening: Path) -> _LegacyJD:
        relative = self._relative(screening)
        screening_content = screening.read_text(encoding="utf-8")
        stem = screening.stem.removeprefix("headhunter-")
        ascii_slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
        suffix = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:8]
        job_id = f"{ascii_slug or 'posting'}-{suffix}"
        metadata = extract_metadata_from_jd(screening_content)
        company = metadata.get("company") or self._heading(screening_content) or "비공개 헤드헌터 공고"
        position = metadata.get("position") or "Backend"
        placeholder = (
            f"# {company.strip()} - {position.strip()}\n\n"
            "> 원본 JD 파일이 레거시 저장소에 없어 스크리닝 결과만 이관되었습니다.\n"
        )
        record = JobRecord(
            platform="headhunter",
            job_id=job_id,
            company=company.strip(),
            position=position.strip(),
            screening_verdict=to_screening_verdict(
                parse_verdict_from_screening(screening_content) or ""
            ),
            migration_source=relative,
        )
        return _LegacyJD(
            path=None,
            relative=None,
            record=record,
            content=placeholder,
        )

    def _axes(
        self, path: Path, content: str
    ) -> tuple[ScreeningVerdict | None, ApplicationStatus, PostingStatus, str | None]:
        relative_parts = set(path.relative_to(self.paths.legacy_private / "job_postings").parts[:-1])
        verdict: ScreeningVerdict | None = None
        if "pass" in relative_parts:
            verdict = ScreeningVerdict.NOT_RECOMMENDED
        elif "conditional" in relative_parts:
            verdict = (
                ScreeningVerdict.RECOMMENDED
                if "high" in relative_parts
                else ScreeningVerdict.HOLD
            )
        frontmatter = parse_frontmatter(content)
        normalized = normalize_status(frontmatter.get("status"))
        application = ApplicationStatus.PENDING
        updated: str | None = None
        if normalized in {item.value for item in ApplicationStatus} and normalized != "pending":
            application = ApplicationStatus(normalized)
            updated = frontmatter.get("status_updated") or self._mtime(path)
        elif "applied" in relative_parts:
            application = ApplicationStatus.APPLIED
            updated = self._mtime(path)
        elif "rejected" in relative_parts:
            application = ApplicationStatus.REJECTED
            updated = self._mtime(path)
        posting = PostingStatus.CLOSED if "closed" in relative_parts else PostingStatus.ACTIVE
        return verdict, application, posting, updated

    def _stage_owned_files(
        self, files: Iterable[Path], identity: dict[str, set[str]]
    ) -> list[MigrationFinding]:
        blockers: list[MigrationFinding] = []
        for source in files:
            relative = self._relative(source)
            if source.name == "search_config.yaml" or source.name == "jd-screening-rules.md":
                destination = self.paths.stage_root / "config" / source.name
                try:
                    self._copy_bytes(source, destination)
                except OSError as exc:
                    blockers.append(MigrationFinding("config_copy_failed", relative, str(exc)))
                continue
            if source.name == "queue.json":
                destination = self.paths.stage_root / "runtime/queue/queue.json"
            elif source.name == ".search_state.json":
                destination = self.paths.stage_root / "runtime/search_state.json"
            elif source.name.startswith(".auto_state_"):
                destination = self.paths.stage_root / "runtime/auto/state" / source.name
            elif source.name.startswith("auto_") and source.suffix == ".json":
                destination = self.paths.stage_root / "runtime/auto/results" / source.name
            elif source.name.startswith("search_") and source.suffix == ".json":
                destination = self.paths.stage_root / "runtime/search/results" / source.name
            elif source.name == "company_enrichment_thevc.txt":
                destination = self.paths.stage_root / "runtime/company_enrichment/thevc.txt"
            elif source.name == "company_enrichment_saramin.txt":
                destination = self.paths.stage_root / "runtime/company_enrichment/saramin.txt"
            elif source.name.startswith("search_") and source.suffix == ".txt":
                destination = self.paths.stage_root / "runtime/search/requests" / source.name
            else:
                blockers.append(MigrationFinding("runtime_owner_unknown", relative, source.name))
                continue
            try:
                if source.suffix == ".txt":
                    self._copy_bytes(source, destination)
                    continue
                payload = json.loads(source.read_text(encoding="utf-8"))
                rewritten, errors = self._rewrite_runtime(payload, identity, path=relative)
                blockers.extend(errors)
                self._atomic_json(destination, rewritten)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                blockers.append(MigrationFinding("runtime_read_failed", relative, str(exc)))
        return blockers

    def _rewrite_runtime(
        self, value: Any, identity: dict[str, set[str]], *, path: str
    ) -> tuple[Any, list[MigrationFinding]]:
        errors: list[MigrationFinding] = []
        if isinstance(value, dict):
            rewritten: dict[str, Any] = {}
            item_platform: str | None = None
            item_id = self._normal_job_id(str(value.get("job_id", ""))) or None
            url = str(value.get("url", ""))
            explicit_platform = value.get("platform")
            if explicit_platform in _KNOWN_PLATFORMS:
                item_platform = str(explicit_platform)
            elif url:
                item_platform = get_platform_from_url(url)
            if item_id and item_platform is None and self._is_legacy_job_identity(item_id):
                legacy_platform, legacy_id = self._runtime_key(str(value.get("job_id", "")))
                item_id = legacy_id or item_id
                item_platform = legacy_platform or "wanted"
            for key, child in value.items():
                if key in _ID_LIST_KEYS and isinstance(child, list):
                    composite: list[str] = []
                    for raw_id in child:
                        platform, job_id = self._runtime_key(str(raw_id))
                        if not job_id:
                            continue
                        platform = platform or "wanted"
                        composite.append(f"{platform}:{job_id}")
                    output_key = "seen_job_keys" if key == "seen_job_ids" else key
                    rewritten[output_key] = sorted(composite)
                elif key == "items" and isinstance(child, dict):
                    new_items: dict[str, Any] = {}
                    for raw_key, item in sorted(child.items()):
                        key_platform, job_id = self._runtime_key(str(raw_key))
                        if key_platform and isinstance(item, dict):
                            item = {"job_id": job_id, "platform": key_platform, **item}
                        child_value, child_errors = self._rewrite_runtime(item, identity, path=path)
                        errors.extend(child_errors)
                        platform = (
                            child_value.get("platform")
                            if isinstance(child_value, dict)
                            else key_platform
                        )
                        platform = platform or key_platform
                        if job_id and platform:
                            new_items[f"{platform}:{job_id}"] = child_value
                        else:
                            new_items[str(raw_key)] = child_value
                    rewritten[key] = new_items
                else:
                    rewritten[key], child_errors = self._rewrite_runtime(child, identity, path=path)
                    errors.extend(child_errors)
            if item_id and item_platform:
                rewritten["job_id"] = item_id
                rewritten["platform"] = item_platform
            return rewritten, errors
        if isinstance(value, list):
            result = []
            for child in value:
                rewritten, child_errors = self._rewrite_runtime(child, identity, path=path)
                result.append(rewritten)
                errors.extend(child_errors)
            return result, errors
        return value, errors

    def _validate_staged_hashes(
        self, mappings: Iterable[MigrationMapping], source_hashes: dict[str, str]
    ) -> list[MigrationFinding]:
        findings: list[MigrationFinding] = []
        repository = JDRecordRepository(canonical_records_root(self.paths.stage_root))
        for mapping in mappings:
            try:
                stored = repository.get(JobKey(mapping.platform, mapping.job_id))
                if mapping.source is not None:
                    jd_hash = hashlib.sha256(stored.jd_markdown.encode("utf-8")).hexdigest()
                    if jd_hash != source_hashes[mapping.source]:
                        findings.append(MigrationFinding("jd_hash_mismatch", mapping.source, "staged JD bytes differ"))
                if mapping.screening_source:
                    screening_hash = hashlib.sha256((stored.screening_markdown or "").encode("utf-8")).hexdigest()
                    if screening_hash != source_hashes[mapping.screening_source]:
                        findings.append(MigrationFinding("screening_hash_mismatch", mapping.screening_source, "staged screening bytes differ"))
            except (OSError, UnicodeError, ValueError) as exc:
                findings.append(MigrationFinding("stage_validation_failed", mapping.source or mapping.screening_source or "", str(exc)))
        return findings

    def _identity_lookup(self, jds: Iterable[_LegacyJD]) -> dict[str, set[str]]:
        lookup: dict[str, set[str]] = {}
        for jd in jds:
            lookup.setdefault(jd.record.job_id, set()).add(jd.record.platform)
        return lookup

    def _ignored_inputs(self) -> list[Path]:
        postings = self.paths.legacy_private / "job_postings"
        screening_root = self.paths.legacy_private / "jd_analysis" / "screening"
        ignored: set[Path] = set()
        if postings.exists():
            ignored.update(path for path in postings.rglob("CLAUDE.md") if path.is_file())
        for name in ("SUMMARY.md", "PRIORITY.md"):
            candidate = screening_root / name
            if candidate.is_file():
                ignored.add(candidate)
        invalid = screening_root / "invalid"
        if invalid.exists():
            ignored.update(path for path in invalid.rglob("*") if path.is_file())
        dedup_trash = screening_root / ".dedup_trash"
        if dedup_trash.exists():
            ignored.update(path for path in dedup_trash.rglob("*") if path.is_file())
        return sorted(ignored)

    def _reset_stage(self) -> None:
        if self.paths.stage_root.exists():
            shutil.rmtree(self.paths.stage_root)
        self.paths.stage_root.mkdir(parents=True)

    def _write_report(self, report: MigrationReport, *, include_stage: bool = True) -> None:
        self._atomic_json(self.paths.report_path, report.to_dict())
        if include_stage and self.paths.stage_root.exists():
            self._atomic_json(
                self.paths.stage_root / "runtime/migration-report.json",
                report.to_dict(),
            )

    def _atomic_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        self._fsync_dir(path.parent)

    def _copy_bytes(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    def _relative(self, path: Path | None) -> str:
        if path is None:
            return ""
        return path.relative_to(self.paths.legacy_private).as_posix()

    def _filename_platform(self, filename: str) -> str | None:
        prefix = Path(filename).stem.split("-", 1)[0].lower()
        return prefix if prefix in _KNOWN_PLATFORMS else None

    def _filename_has_identity_hint(self, filename: str) -> bool:
        stem = Path(filename).stem
        prefix = stem.split("-", 1)[0].lower()
        return prefix.isdigit() or prefix in _KNOWN_PLATFORMS

    def _numeric_filename_id(self, filename: str) -> str | None:
        prefix = Path(filename).stem.split("-", 1)[0]
        return prefix if prefix.isdigit() else None

    def _private_identity(self, filename: str) -> tuple[str, str, str] | None:
        stem = Path(filename).stem
        if not stem.startswith("private-"):
            return None
        job_id = stem.removeprefix("private-")
        if not job_id:
            return None
        return "private", job_id, ""

    def _normal_job_id(self, job_id: str | None) -> str | None:
        if not job_id:
            return None
        for prefix in ("groupby-", "remember-"):
            job_id = job_id.removeprefix(prefix)
        return job_id

    def _runtime_key(self, value: str) -> tuple[str | None, str | None]:
        if ":" in value:
            platform, job_id = value.split(":", 1)
            if platform in _KNOWN_PLATFORMS:
                return platform, self._normal_job_id(job_id)
        for platform in ("remember", "groupby"):
            prefix = f"{platform}-"
            if value.startswith(prefix):
                return platform, value.removeprefix(prefix)
        return None, self._normal_job_id(value)

    def _is_legacy_job_identity(self, value: str) -> bool:
        return value.isdigit() or any(
            value.startswith(f"{platform}-") for platform in ("remember", "groupby")
        )


    def _safe_finding(self, finding: MigrationFinding) -> MigrationFinding:
        message = finding.message
        replacements = {
            str(self.paths.legacy_private): "<legacy>",
            str(self.paths.stage_root): "<stage>",
            str(self.paths.active_root): "<active>",
            str(self.paths.report_path): "<report>",
        }
        for absolute, label in replacements.items():
            message = message.replace(absolute, label)
        return replace(finding, message=message)

    def _heading(self, content: str) -> str | None:
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else None

    def _mtime(self, path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    def _fsync_dir(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
