"""Rebuildable SQLite metadata index for canonical JD records."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterator

from ...domain.model import ApplicationStatus, PostingStatus, ScreeningVerdict
from .file_records import JDRecordRepository, JobRecordRepositoryError


_SCHEMA = """
CREATE TABLE job_records (
    platform TEXT NOT NULL,
    job_id TEXT NOT NULL,
    company TEXT NOT NULL,
    position TEXT NOT NULL,
    source_url TEXT NOT NULL,
    screening_verdict TEXT,
    application_status TEXT NOT NULL,
    posting_status TEXT NOT NULL,
    application_status_updated_at TEXT,
    has_screening INTEGER NOT NULL CHECK (has_screening IN (0, 1)),
    PRIMARY KEY (platform, job_id)
);
CREATE INDEX job_records_by_id ON job_records (job_id, platform);
CREATE INDEX job_records_by_filters ON job_records (
    platform,
    application_status,
    posting_status,
    screening_verdict
);
"""

_INSERT = """
INSERT INTO job_records (
    platform,
    job_id,
    company,
    position,
    source_url,
    screening_verdict,
    application_status,
    posting_status,
    application_status_updated_at,
    has_screening
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class IndexedJobRecord:
    platform: str
    job_id: str
    company: str
    position: str
    source_url: str
    screening_verdict: ScreeningVerdict | None
    application_status: ApplicationStatus
    posting_status: PostingStatus
    application_status_updated_at: str | None
    has_screening: bool


@dataclass(frozen=True)
class SearchResult:
    items: tuple[IndexedJobRecord, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class IndexRebuildError:
    platform: str
    job_id: str
    message: str


@dataclass(frozen=True)
class IndexRebuildReport:
    success: bool
    indexed_count: int
    errors: tuple[IndexRebuildError, ...] = ()


class JDSearchIndex:
    """Query facade over a derived database rebuilt from file records."""

    def __init__(self, database_path: str | Path, repository: JDRecordRepository) -> None:
        self.database_path = Path(database_path)
        self.repository = repository

    def rebuild(self) -> IndexRebuildReport:
        """Build a complete temporary database and publish it only when valid."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked_rebuild():
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{self.database_path.name}.",
                suffix=".tmp",
                dir=self.database_path.parent,
            )
            os.close(descriptor)
            temp_path = Path(temp_name)
            errors: list[IndexRebuildError] = []
            indexed_count = 0

            try:
                with sqlite3.connect(temp_path) as connection:
                    connection.executescript(_SCHEMA)
                    for key in self.repository.iter_keys():
                        try:
                            stored = self.repository.validate_integrity(key)
                        except (JobRecordRepositoryError, OSError, UnicodeError, ValueError) as exc:
                            errors.append(
                                IndexRebuildError(
                                    platform=key.platform,
                                    job_id=key.job_id,
                                    message=str(exc),
                                )
                            )
                            continue

                        record = stored.record
                        connection.execute(
                            _INSERT,
                            (
                                record.platform,
                                record.job_id,
                                record.company,
                                record.position,
                                record.source_url,
                                (
                                    record.screening_verdict.value
                                    if record.screening_verdict is not None
                                    else None
                                ),
                                record.application_status.value,
                                record.posting_status.value,
                                record.application_status_updated_at,
                                int(stored.has_screening),
                            ),
                        )
                        indexed_count += 1

                if errors:
                    return IndexRebuildReport(success=False, indexed_count=0, errors=tuple(errors))

                self._fsync_file(temp_path)
                temp_path.replace(self.database_path)
                self._fsync_directory(self.database_path.parent)
                return IndexRebuildReport(success=True, indexed_count=indexed_count)
            finally:
                temp_path.unlink(missing_ok=True)

    def search(
        self,
        *,
        job_id: str | None = None,
        platform: str | None = None,
        screening_verdict: ScreeningVerdict | str | None = None,
        application_status: ApplicationStatus | str | None = None,
        posting_status: PostingStatus | str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchResult:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if offset < 0:
            raise ValueError("offset must not be negative")

        clauses: list[str] = []
        parameters: list[str] = []
        if job_id is not None and job_id != "":
            clauses.append("job_id = ?")
            parameters.append(job_id)
        if platform is not None:
            clauses.append("platform = ?")
            parameters.append(platform)
        if screening_verdict == "null":
            clauses.append("screening_verdict IS NULL")
        elif screening_verdict is not None:
            clauses.append("screening_verdict = ?")
            parameters.append(_enum_value(screening_verdict))
        if application_status is not None:
            clauses.append("application_status = ?")
            parameters.append(_enum_value(application_status))
        if posting_status is not None:
            clauses.append("posting_status = ?")
            parameters.append(_enum_value(posting_status))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        count_sql = f"SELECT COUNT(*) FROM job_records{where}"
        select_sql = f"""
            SELECT
                platform,
                job_id,
                company,
                position,
                source_url,
                screening_verdict,
                application_status,
                posting_status,
                application_status_updated_at,
                has_screening
            FROM job_records
            {where}
            ORDER BY job_id ASC, platform ASC
            LIMIT ? OFFSET ?
        """

        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            total = int(connection.execute(count_sql, parameters).fetchone()[0])
            rows = connection.execute(select_sql, (*parameters, limit, offset)).fetchall()

        return SearchResult(
            items=tuple(_indexed_record(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    def _fsync_file(self, path: Path) -> None:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _lock_path(self) -> Path:
        return self.database_path.with_name(f".{self.database_path.name}.lock")

    @contextmanager
    def _locked_rebuild(self) -> Iterator[None]:
        lock_path = self._lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _enum_value(value: ScreeningVerdict | ApplicationStatus | PostingStatus | str) -> str:
    return value.value if isinstance(value, (ScreeningVerdict, ApplicationStatus, PostingStatus)) else value


def _indexed_record(row: sqlite3.Row) -> IndexedJobRecord:
    verdict = row["screening_verdict"]
    return IndexedJobRecord(
        platform=str(row["platform"]),
        job_id=str(row["job_id"]),
        company=str(row["company"]),
        position=str(row["position"]),
        source_url=str(row["source_url"]),
        screening_verdict=ScreeningVerdict(str(verdict)) if verdict is not None else None,
        application_status=ApplicationStatus(str(row["application_status"])),
        posting_status=PostingStatus(str(row["posting_status"])),
        application_status_updated_at=(
            str(row["application_status_updated_at"])
            if row["application_status_updated_at"] is not None
            else None
        ),
        has_screening=bool(row["has_screening"]),
    )
