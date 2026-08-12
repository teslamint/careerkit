"""Loopback-only HTTP API and static server for canonical JD records."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.resources as resources
import ipaddress
import json
from pathlib import Path
import sqlite3
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordNotFound,
    StoredJobRecord,
    JobRecordRepositoryError,
)
from careerkit.jobs.adapters.storage.sqlite_index import IndexedJobRecord, JDSearchIndex
from careerkit.jobs.domain.model import ApplicationStatus, JobKey

_STATIC_PACKAGE = "careerkit.jobs.console.static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_MAX_PATCH_BODY_BYTES = 8 * 1024
_PATCH_ALLOWED_FIELDS = {"application_status", "occurred_at", "note"}
_INDEX_REFRESH_EXCEPTIONS = (
    JobRecordRepositoryError,
    OSError,
    UnicodeError,
    ValueError,
    sqlite3.Error,
)


class RecordStatusService(Protocol):
    repository: JDRecordRepository

    def set_record_status(
        self,
        key: JobKey,
        *,
        application_status: ApplicationStatus | None = None,
        posting_status: Any = None,
        application_status_updated_at: str | None = None,
        application_note: str | None = None,
    ) -> StoredJobRecord: ...


class JDConsoleServer(ThreadingHTTPServer):
    repository: JDRecordRepository
    search_index: JDSearchIndex
    pipeline_service: RecordStatusService


def create_server(
    *,
    records_root: str | Path,
    database_path: str | Path,
    pipeline_service: RecordStatusService,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> JDConsoleServer:
    if not is_loopback(host):
        raise ValueError("JD console must bind to a loopback host")

    repository_root = Path(records_root).resolve(strict=False)
    if pipeline_service.repository.root.resolve(strict=False) != repository_root:
        raise ValueError("pipeline repository root must match records_root")

    repository = JDRecordRepository(repository_root)
    search_index = JDSearchIndex(database_path, repository)
    report = search_index.rebuild()
    if not report.success:
        raise RuntimeError(f"Search index rebuild failed for {len(report.errors)} record(s)")

    server = JDConsoleServer((host, port), _RequestHandler)
    server.repository = repository
    server.search_index = search_index
    server.pipeline_service = pipeline_service
    return server


class _RequestHandler(BaseHTTPRequestHandler):
    server: JDConsoleServer

    def do_GET(self) -> None:  # noqa: N802
        if self._validated_host_header() is None:
            self._json_error(HTTPStatus.FORBIDDEN, "invalid host")
            return

        parsed = urlsplit(self.path)
        if parsed.path == "/api/jobs":
            self._search(parse_qs(parsed.query, keep_blank_values=True))
            return
        if parsed.path.startswith("/api/jobs/"):
            self._detail(parsed.path)
            return
        static = _STATIC_FILES.get(parsed.path)
        if static is not None:
            self._static(*static)
            return
        self._json_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if not _is_status_route(parsed.path):
            self._method_not_allowed()
            return

        host = self._validated_host_header()
        if host is None:
            self._json_error(HTTPStatus.FORBIDDEN, "invalid host")
            return
        if not self._valid_origin(host):
            self._json_error(HTTPStatus.FORBIDDEN, "invalid origin")
            return

        try:
            key = self._status_route_key(parsed.path)
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        try:
            content_length = self._validated_content_length()
            content_type = self._validated_content_type()
        except _RequestValidationError as exc:
            self._json_error(exc.status, exc.message)
            return

        if content_type != "application/json":
            self._json_error(HTTPStatus.BAD_REQUEST, "content type must be application/json")
            return

        try:
            payload = self._load_patch_payload(content_length)
            occurred_at = _optional_patch_string(payload.get("occurred_at"), field="occurred_at")
            note = _optional_patch_string(payload.get("note"), field="note")
            application_status = _parse_patch_application_status(
                payload.get("application_status"),
                occurred_at=occurred_at,
                note=note,
            )
            updated = self.server.pipeline_service.set_record_status(
                key,
                application_status=application_status,
                application_status_updated_at=occurred_at,
                application_note=note,
            )
        except JobRecordNotFound:
            self._json_error(HTTPStatus.NOT_FOUND, "record not found")
            return
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, _friendly_status_error(str(exc)))
            return
        except (JobRecordRepositoryError, OSError, UnicodeError, sqlite3.Error):
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "record integrity check failed")
            return

        response = _record_payload(updated)
        try:
            rebuild_report = self.server.search_index.rebuild()
        except _INDEX_REFRESH_EXCEPTIONS:
            rebuild_report = None
        if rebuild_report is not None and rebuild_report.success:
            response["index_refreshed"] = True
            response["index_warning"] = None
        else:
            response["index_refreshed"] = False
            response["index_warning"] = "canonical update saved; search index refresh failed"
        self._json(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _search(self, query: dict[str, list[str]]) -> None:
        try:
            refreshed = _query_bool(query, "refresh")
            if refreshed:
                report = self.server.search_index.rebuild()
                if not report.success:
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": "search index refresh failed", "error_count": len(report.errors)},
                    )
                    return

            result = self.server.search_index.search(
                job_id=_query_value(query, "job_id"),
                platform=_query_value(query, "platform"),
                screening_verdict=_query_value(query, "screening_verdict"),
                application_status=_query_value(query, "application_status"),
                posting_status=_query_value(query, "posting_status"),
                limit=_query_int(query, "limit", 50),
                offset=_query_int(query, "offset", 0),
            )
        except (TypeError, ValueError) as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self._json(
            HTTPStatus.OK,
            {
                "items": [indexed_payload(item) for item in result.items],
                "total": result.total,
                "limit": result.limit,
                "offset": result.offset,
                "refreshed": refreshed,
            },
        )

    def _detail(self, path: str) -> None:
        parts = [unquote(part) for part in path.removeprefix("/api/jobs/").split("/")]
        if len(parts) != 2:
            self._json_error(HTTPStatus.BAD_REQUEST, "detail requires platform and job_id")
            return
        try:
            stored = self.server.repository.get(JobKey(parts[0], parts[1]))
        except ValueError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except JobRecordNotFound:
            self._json_error(HTTPStatus.NOT_FOUND, "record not found")
            return
        except (JobRecordRepositoryError, OSError, UnicodeError):
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "record integrity check failed")
            return

        self._json(HTTPStatus.OK, _record_payload(stored))

    def _static(self, filename: str, content_type: str) -> None:
        body = resources.files(_STATIC_PACKAGE).joinpath(filename).read_bytes()
        self._send(HTTPStatus.OK, body, content_type)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, PATCH")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _validated_host_header(self) -> str | None:
        values = self.headers.get_all("Host")
        if values is None or len(values) != 1:
            return None
        value = values[0].strip()
        if not value:
            return None
        parsed = urlsplit(f"//{value}")
        if parsed.username is not None or parsed.password is not None:
            return None
        hostname = parsed.hostname
        if hostname is None or not is_loopback(hostname):
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        host = hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{port}" if port is not None else host

    def _valid_origin(self, host: str) -> bool:
        values = self.headers.get_all("Origin")
        if values is None or len(values) != 1:
            return False
        return values[0].strip() == f"http://{host}"

    def _validated_content_type(self) -> str:
        values = self.headers.get_all("Content-Type")
        if values is None or len(values) != 1:
            raise _RequestValidationError(HTTPStatus.BAD_REQUEST, "content type must be application/json")
        return values[0].split(";", 1)[0].strip().lower()

    def _validated_content_length(self) -> int:
        values = self.headers.get_all("Content-Length")
        if values is None or len(values) != 1:
            raise _RequestValidationError(HTTPStatus.BAD_REQUEST, "content length is required")
        try:
            length = int(values[0])
        except ValueError as exc:
            raise _RequestValidationError(HTTPStatus.BAD_REQUEST, "content length must be an integer") from exc
        if length < 0:
            raise _RequestValidationError(HTTPStatus.BAD_REQUEST, "content length must not be negative")
        if length > _MAX_PATCH_BODY_BYTES:
            raise _RequestValidationError(HTTPStatus.BAD_REQUEST, "request body must be 8192 bytes or fewer")
        return length

    def _status_route_key(self, path: str) -> JobKey:
        parts = [unquote(part) for part in path.removeprefix("/api/jobs/").split("/")]
        if len(parts) != 3 or parts[2] != "application-status":
            raise ValueError("detail requires platform and job_id")
        return JobKey(parts[0], parts[1])

    def _load_patch_payload(self, content_length: int) -> dict[str, Any]:
        try:
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        unknown_fields = sorted(set(payload) - _PATCH_ALLOWED_FIELDS)
        if unknown_fields:
            raise ValueError(
                "request body must contain only application_status, occurred_at, and note"
            )
        return payload


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_status_route(path: str) -> bool:
    parts = [unquote(part) for part in path.removeprefix("/api/jobs/").split("/")]
    return len(parts) == 3 and parts[2] == "application-status"


def _query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _query_int(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _query_value(query, key)
    return default if value is None or value == "" else int(value)


def _query_bool(query: dict[str, list[str]], key: str) -> bool:
    value = _query_value(query, key)
    if value is None or value == "":
        return False
    if value == "1":
        return True
    raise ValueError(f"{key} must be 1 when provided")


def indexed_payload(item: IndexedJobRecord) -> dict[str, Any]:
    payload = asdict(item)
    payload["screening_verdict"] = item.screening_verdict.value if item.screening_verdict is not None else None
    payload["application_status"] = item.application_status.value
    payload["posting_status"] = item.posting_status.value
    return payload


class _RequestValidationError(ValueError):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _record_payload(stored: StoredJobRecord) -> dict[str, Any]:
    payload = stored.record.to_dict()
    payload.update(
        {
            "jd_markdown": stored.jd_markdown,
            "screening_markdown": stored.screening_markdown,
            "has_screening": stored.screening_markdown is not None,
        }
    )
    return payload


def _optional_patch_string(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string when provided")
    return value


def _parse_patch_application_status(
    value: Any,
    *,
    occurred_at: str | None,
    note: str | None,
) -> ApplicationStatus:
    if value is None:
        if note is not None:
            raise ValueError("application status is required when application_note is set")
        if occurred_at is not None:
            raise ValueError(
                "application status is required when application_status_updated_at is set"
            )
        raise ValueError("application_status is required")
    if not isinstance(value, str):
        raise ValueError("application_status must be a string when provided")
    try:
        return ApplicationStatus(value)
    except ValueError as exc:
        raise ValueError("invalid application_status") from exc


def _friendly_status_error(message: str) -> str:
    return (
        message
        .replace(
            "application status is required when application_note is set",
            "application note requires application_status",
        )
        .replace(
            "application status is required when application_status_updated_at is set",
            "occurred_at requires application_status",
        )
    )
