"""File-backed canonical repository for JD records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ...domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    SCHEMA_VERSION,
    ScreeningVerdict,
)


_MANIFEST_NAME = "record.json"
_LOCK_NAME = ".lock"
_CONTENT_DIR = "content"
_JD_FILE_NAME = "jd.md"
_SCREENING_FILE_NAME = "screening.md"


def canonical_records_root(root: str | Path) -> Path:
    return Path(root) / "records"


class JobRecordRepositoryError(Exception):
    """Base repository exception."""


class JobRecordNotFound(JobRecordRepositoryError, FileNotFoundError):
    """Raised when a record does not exist."""


class JobRecordIntegrityError(JobRecordRepositoryError):
    """Raised when persisted data is missing or corrupted."""


@dataclass(frozen=True)
class StoredJobRecord:
    record: JobRecord
    jd_markdown: str
    screening_markdown: str | None = None


@dataclass(frozen=True)
class StoredJobMetadata:
    record: JobRecord
    has_screening: bool


@dataclass(frozen=True)
class _ManifestContent:
    revision: str
    jd_path: str
    jd_sha256: str
    screening_path: str | None
    screening_sha256: str | None


class JDRecordRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def create(self, record: JobRecord, *, jd_markdown: str) -> StoredJobRecord:
        return self._save(record, jd_markdown=jd_markdown, allow_overwrite=False)

    def save(self, record: JobRecord, *, jd_markdown: str) -> StoredJobRecord:
        return self._save(record, jd_markdown=jd_markdown, allow_overwrite=True)

    def get(self, key: JobKey) -> StoredJobRecord:
        record_dir = self._record_dir(key)
        if not record_dir.exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=False):
            record = self._read_locked(key, record_dir)
            if record is None:
                raise JobRecordNotFound(f"Record not found: {key!r}")
            return record

    def find(self, key: JobKey) -> StoredJobRecord | None:
        record_dir = self._record_dir(key)
        if not record_dir.exists():
            return None
        return self.get(key)

    read = get

    def iter_keys(self) -> Iterator[JobKey]:
        """Yield canonical record keys without loading Markdown content."""
        if not self.root.exists():
            return
        for platform_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for job_dir in sorted(p for p in platform_dir.iterdir() if p.is_dir()):
                yield JobKey(platform_dir.name, job_dir.name)

    def list(self) -> list[StoredJobRecord]:
        records = [self.get(key) for key in self.iter_keys()]
        records.sort(key=lambda item: item.record.key)
        return records

    def get_metadata(self, key: JobKey) -> StoredJobMetadata:
        """Read index/report metadata without loading Markdown bodies."""
        record_dir = self._record_dir(key)
        if not record_dir.exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=False):
            manifest = self._load_manifest(record_dir)
            record = self._record_from_manifest(manifest)
            if record.key != key:
                raise JobRecordIntegrityError(
                    f"Manifest identity mismatch: expected {key!r}, got {record.key!r}"
                )
            content = self._parse_manifest_content(manifest)
            if content.screening_path is not None and content.screening_sha256 is None:
                raise JobRecordIntegrityError("Invalid manifest: screening_sha256 missing")
            return StoredJobMetadata(
                record=record,
                has_screening=content.screening_path is not None,
            )

    def list_metadata(self) -> list[StoredJobMetadata]:
        metadata = [self.get_metadata(key) for key in self.iter_keys()]
        metadata.sort(key=lambda item: item.record.key)
        return metadata

    def validate_integrity(self, key: JobKey) -> StoredJobMetadata:
        """Verify manifest identity and referenced content hashes without returning bodies."""
        record_dir = self._record_dir(key)
        if not record_dir.exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=False):
            manifest = self._load_manifest(record_dir)
            record = self._record_from_manifest(manifest)
            if record.key != key:
                raise JobRecordIntegrityError(
                    f"Manifest identity mismatch: expected {key!r}, got {record.key!r}"
                )
            content = self._parse_manifest_content(manifest)
            self._read_and_verify_content(record_dir, content.jd_path, content.jd_sha256)
            if content.screening_path is not None:
                if content.screening_sha256 is None:
                    raise JobRecordIntegrityError("Invalid manifest: screening_sha256 missing")
                self._read_and_verify_content(
                    record_dir,
                    content.screening_path,
                    content.screening_sha256,
                )
            return StoredJobMetadata(
                record=record,
                has_screening=content.screening_path is not None,
            )

    def update_screening_result(
        self,
        key: JobKey,
        *,
        screening_markdown: str,
        screening_verdict: ScreeningVerdict | None = None,
        screening_provider: str | None = None,
        verdict_capped: bool | None = None,
    ) -> StoredJobRecord:
        """Atomically publish screening content against the latest record metadata."""
        record_dir = self._record_dir(key)
        if not self._manifest_path(record_dir).exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=True):
            current = self._read_existing_locked(key, record_dir)
            content = self._write_revision_content(
                record_dir,
                jd_markdown=current.jd_markdown,
                screening_markdown=screening_markdown,
            )
            updated_record = replace(
                current.record,
                screening_verdict=(
                    screening_verdict
                    if screening_verdict is not None
                    else current.record.screening_verdict
                ),
                screening_provider=(
                    screening_provider
                    if screening_provider is not None
                    else current.record.screening_provider
                ),
                verdict_capped=(
                    verdict_capped
                    if verdict_capped is not None
                    else current.record.verdict_capped
                ),
            )
            self._publish_manifest(record_dir, updated_record, content)
            self._cleanup_stale_revisions(record_dir, keep_revision=content.revision)
            return StoredJobRecord(
                record=updated_record,
                jd_markdown=current.jd_markdown,
                screening_markdown=screening_markdown,
            )

    def update_verdict(
        self,
        key: JobKey,
        screening_verdict: ScreeningVerdict,
        *,
        prescreen_reason: str | None = None,
    ) -> StoredJobRecord:
        """Update only verdict metadata while preserving newer status fields and content."""
        record_dir = self._record_dir(key)
        if not self._manifest_path(record_dir).exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=True):
            current = self._read_existing_locked(key, record_dir)
            updated_record = replace(
                current.record,
                screening_verdict=screening_verdict,
                prescreen_reason=prescreen_reason if prescreen_reason is not None else current.record.prescreen_reason,
            )
            content = self._load_manifest_content(record_dir)
            self._publish_manifest(record_dir, updated_record, content)
            return replace(current, record=updated_record)

    def update_prescreen(self, key: JobKey, reason: str) -> StoredJobRecord:
        """Record why screening was skipped, leaving the verdict field untouched."""
        record_dir = self._record_dir(key)
        if not self._manifest_path(record_dir).exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=True):
            current = self._read_existing_locked(key, record_dir)
            updated_record = replace(current.record, prescreen_reason=reason)
            content = self._load_manifest_content(record_dir)
            self._publish_manifest(record_dir, updated_record, content)
            return replace(current, record=updated_record)

    def update_status(
        self,
        key: JobKey,
        *,
        application_status: ApplicationStatus | None = None,
        posting_status: PostingStatus | None = None,
        application_status_updated_at: str | None = None,
        application_note: str | None = None,
    ) -> StoredJobRecord:
        event = self._build_application_event(
            application_status=application_status,
            application_status_updated_at=application_status_updated_at,
            application_note=application_note,
        )
        record_dir = self._record_dir(key)
        if not self._manifest_path(record_dir).exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=True):
            current = self._read_existing_locked(key, record_dir)
            application_history = current.record.application_history
            if event is not None:
                application_history = (*application_history, event)
            updated_record = replace(
                current.record,
                application_status=(
                    event.status if event is not None else current.record.application_status
                ),
                posting_status=posting_status or current.record.posting_status,
                application_status_updated_at=(
                    event.occurred_at
                    if event is not None
                    else current.record.application_status_updated_at
                ),
                application_history=application_history,
            )
            content = self._load_manifest_content(record_dir)
            self._publish_manifest(record_dir, updated_record, content)
            return StoredJobRecord(
                record=updated_record,
                jd_markdown=current.jd_markdown,
                screening_markdown=current.screening_markdown,
            )

    def _save(
        self,
        record: JobRecord,
        *,
        jd_markdown: str,
        allow_overwrite: bool,
    ) -> StoredJobRecord:
        record_dir = self._record_dir(record.key)
        with self._locked(record_dir, exclusive=True):
            current = self._read_locked(record.key, record_dir, missing_ok=True)
            if current is not None and not allow_overwrite:
                raise FileExistsError(f"Record already exists: {record.key!r}")
            locked_record = (
                self._merge_refresh_record(record, current.record)
                if current is not None
                else record
            )
            screening_markdown = current.screening_markdown if current is not None else None
            content = self._write_revision_content(
                record_dir,
                jd_markdown=jd_markdown,
                screening_markdown=screening_markdown,
            )
            self._publish_manifest(record_dir, locked_record, content)
            self._cleanup_stale_revisions(record_dir, keep_revision=content.revision)
            return StoredJobRecord(
                record=locked_record,
                jd_markdown=jd_markdown,
                screening_markdown=screening_markdown,
            )

    def _record_dir(self, key: JobKey) -> Path:
        return self.root / key.platform / key.job_id

    def _lock_path(self, record_dir: Path) -> Path:
        return record_dir / _LOCK_NAME

    def _manifest_path(self, record_dir: Path) -> Path:
        return record_dir / _MANIFEST_NAME

    def _content_root(self, record_dir: Path) -> Path:
        return record_dir / _CONTENT_DIR

    def _read_existing_locked(self, key: JobKey, record_dir: Path) -> StoredJobRecord:
        record = self._read_locked(key, record_dir)
        if record is None:
            raise JobRecordNotFound(f"Record not found: {key!r}")
        return record

    def _build_application_event(
        self,
        *,
        application_status: ApplicationStatus | None,
        application_status_updated_at: str | None,
        application_note: str | None,
    ) -> ApplicationEvent | None:
        if application_status is None:
            if application_status_updated_at is not None:
                raise ValueError(
                    "application status is required when application_status_updated_at is set"
                )
            if application_note is not None:
                normalized_note = application_note.strip()
                if normalized_note:
                    raise ValueError(
                        "application status is required when application_note is set"
                    )
            return None
        occurred_at = (
            application_status_updated_at
            if application_status_updated_at is not None
            else datetime.now(timezone.utc).astimezone().isoformat()
        )
        return ApplicationEvent(
            status=application_status,
            occurred_at=occurred_at,
            note=application_note,
        )

    def _merge_refresh_record(self, refreshed: JobRecord, current: JobRecord) -> JobRecord:
        return replace(
            current,
            company=refreshed.company,
            position=refreshed.position,
            source_url=refreshed.source_url,
        )

    def screening_path(self, key: JobKey) -> Path | None:
        record_dir = self._record_dir(key)
        if not record_dir.exists():
            raise JobRecordNotFound(f"Record not found: {key!r}")
        with self._locked(record_dir, exclusive=False):
            content = self._load_manifest_content(record_dir)
            if content.screening_path is None:
                return None
            return self._resolve_content_path(record_dir, content.screening_path)

    def _read_locked(
        self,
        key: JobKey,
        record_dir: Path,
        *,
        missing_ok: bool = False,
    ) -> StoredJobRecord | None:
        manifest_path = self._manifest_path(record_dir)
        if not manifest_path.exists():
            if missing_ok:
                return None
            if not record_dir.exists():
                return None
            raise JobRecordIntegrityError(f"Manifest not found for {key.platform}/{key.job_id}")

        manifest = self._load_manifest(record_dir)
        record = self._record_from_manifest(manifest)
        if record.key != key:
            raise JobRecordIntegrityError(
                f"Manifest identity mismatch: expected {key!r}, got {record.key!r}"
            )
        content = self._parse_manifest_content(manifest)
        jd_markdown = self._read_and_verify_content(record_dir, content.jd_path, content.jd_sha256)
        screening_markdown = None
        if content.screening_path is not None:
            if content.screening_sha256 is None:
                raise JobRecordIntegrityError("Invalid manifest: screening_sha256 missing")
            screening_markdown = self._read_and_verify_content(
                record_dir,
                content.screening_path,
                content.screening_sha256,
            )
        return StoredJobRecord(
            record=record,
            jd_markdown=jd_markdown,
            screening_markdown=screening_markdown,
        )

    def _load_manifest(self, record_dir: Path) -> dict[str, Any]:
        manifest_path = self._manifest_path(record_dir)
        try:
            manifest = json.loads(manifest_path.read_text())
        except FileNotFoundError as exc:
            raise JobRecordIntegrityError(f"Manifest not found for {record_dir}") from exc
        except json.JSONDecodeError as exc:
            raise JobRecordIntegrityError(f"Invalid manifest: {exc.msg}") from exc
        if not isinstance(manifest, dict):
            raise JobRecordIntegrityError("Invalid manifest: manifest root must be an object")
        return manifest

    def _record_from_manifest(self, manifest: dict[str, Any]) -> JobRecord:
        try:
            raw_record = manifest["record"]
        except KeyError as exc:
            raise JobRecordIntegrityError("Invalid manifest: missing record") from exc
        if not isinstance(raw_record, dict):
            raise JobRecordIntegrityError("Invalid manifest: record must be an object")

        outer_version = self._manifest_schema_version(manifest.get("schema_version"))
        inner_version = self._manifest_schema_version(raw_record.get("schema_version"))
        if outer_version != inner_version:
            raise JobRecordIntegrityError("Invalid manifest: manifest schema_version mismatch")

        try:
            return JobRecord.from_dict(raw_record)
        except ValueError as exc:
            raise JobRecordIntegrityError(f"Invalid manifest record: {exc}") from exc

    def _manifest_schema_version(self, value: Any) -> int:
        if type(value) is not int:
            raise JobRecordIntegrityError("Invalid manifest: schema_version must be an integer")
        if value not in {1, SCHEMA_VERSION}:
            raise JobRecordIntegrityError("Invalid manifest: unsupported manifest schema_version")
        return value

    def _load_manifest_content(self, record_dir: Path) -> _ManifestContent:
        return self._parse_manifest_content(self._load_manifest(record_dir))

    def _parse_manifest_content(self, manifest: dict[str, Any]) -> _ManifestContent:
        try:
            content = manifest["content"]
            revision = str(content["revision"])
            jd_path = str(content["jd_path"])
            jd_sha256 = str(content["jd_sha256"])
            screening_path_raw = content.get("screening_path")
            screening_sha_raw = content.get("screening_sha256")
        except (KeyError, TypeError) as exc:
            raise JobRecordIntegrityError(f"Invalid manifest: {exc}") from exc

        screening_path = None if screening_path_raw is None else str(screening_path_raw)
        screening_sha256 = None if screening_sha_raw is None else str(screening_sha_raw)
        return _ManifestContent(
            revision=revision,
            jd_path=jd_path,
            jd_sha256=jd_sha256,
            screening_path=screening_path,
            screening_sha256=screening_sha256,
        )

    def _read_and_verify_content(self, record_dir: Path, relative_path: str, expected_sha256: str) -> str:
        path = self._resolve_content_path(record_dir, relative_path)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise JobRecordIntegrityError(f"Content file missing: {relative_path}") from exc
        actual_sha256 = hashlib.sha256(data).hexdigest()
        if actual_sha256 != expected_sha256:
            raise JobRecordIntegrityError(
                f"Hash mismatch for {relative_path}: expected {expected_sha256}, got {actual_sha256}"
            )
        return data.decode("utf-8")

    def _resolve_content_path(self, record_dir: Path, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise JobRecordIntegrityError(f"Invalid manifest path: {relative_path}")
        resolved = (record_dir / relative).resolve(strict=False)
        try:
            resolved.relative_to(record_dir.resolve(strict=False))
        except ValueError as exc:
            raise JobRecordIntegrityError(f"Invalid manifest path: {relative_path}") from exc
        return resolved

    def _write_revision_content(
        self,
        record_dir: Path,
        *,
        jd_markdown: str,
        screening_markdown: str | None,
    ) -> _ManifestContent:
        revision = uuid4().hex
        content_root = self._content_root(record_dir)
        revision_dir = content_root / revision
        revision_dir.mkdir(parents=True, exist_ok=False)

        screening_sha256: str | None = None
        self._write_content_file(revision_dir / _JD_FILE_NAME, jd_markdown)
        if screening_markdown is not None:
            self._write_content_file(revision_dir / _SCREENING_FILE_NAME, screening_markdown)
            screening_sha256 = self._sha256_text(screening_markdown)

        self._fsync_dir(revision_dir)
        self._fsync_dir(content_root)
        self._fsync_dir(record_dir)

        return _ManifestContent(
            revision=revision,
            jd_path=f"{_CONTENT_DIR}/{revision}/{_JD_FILE_NAME}",
            jd_sha256=self._sha256_text(jd_markdown),
            screening_path=(
                f"{_CONTENT_DIR}/{revision}/{_SCREENING_FILE_NAME}"
                if screening_markdown is not None
                else None
            ),
            screening_sha256=screening_sha256,
        )

    def _write_content_file(self, path: Path, content: str) -> None:
        data = content.encode("utf-8")
        with path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _publish_manifest(self, record_dir: Path, record: JobRecord, content: _ManifestContent) -> None:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record": record.to_dict(),
            "content": {
                "revision": content.revision,
                "jd_path": content.jd_path,
                "jd_sha256": content.jd_sha256,
                "screening_path": content.screening_path,
                "screening_sha256": content.screening_sha256,
            },
        }
        manifest_path = self._manifest_path(record_dir)
        temp_path = manifest_path.with_suffix(".tmp")
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(manifest_path)
        self._fsync_dir(record_dir)

    def _cleanup_stale_revisions(self, record_dir: Path, *, keep_revision: str) -> None:
        content_root = self._content_root(record_dir)
        if not content_root.exists():
            return
        for candidate in content_root.iterdir():
            if candidate.name == keep_revision:
                continue
            self._remove_tree(candidate)
        self._fsync_dir(content_root)
        self._fsync_dir(record_dir)

    def _remove_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for current_root, dir_names, file_names in os.walk(root, topdown=False):
            current = Path(current_root)
            for file_name in file_names:
                (current / file_name).unlink(missing_ok=True)
            for dir_name in dir_names:
                (current / dir_name).rmdir()
        root.rmdir()

    @contextmanager
    def _locked(self, record_dir: Path, *, exclusive: bool) -> Iterator[None]:
        record_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._lock_path(record_dir)
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _fsync_dir(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _sha256_text(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
