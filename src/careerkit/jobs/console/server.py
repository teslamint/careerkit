"""Loopback-only HTTP API and static server for canonical JD records."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.resources as resources
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from careerkit.jobs.adapters.storage.file_records import (
    JDRecordRepository,
    JobRecordNotFound,
    JobRecordRepositoryError,
)
from careerkit.jobs.adapters.storage.sqlite_index import IndexedJobRecord, JDSearchIndex
from careerkit.jobs.domain.model import JobKey

_STATIC_PACKAGE = "careerkit.jobs.console.static"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class JDConsoleServer(ThreadingHTTPServer):
    repository: JDRecordRepository
    search_index: JDSearchIndex


def create_server(
    *,
    records_root: str | Path,
    database_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> JDConsoleServer:
    if not is_loopback(host):
        raise ValueError("JD console must bind to a loopback host")

    repository = JDRecordRepository(records_root)
    search_index = JDSearchIndex(database_path, repository)
    report = search_index.rebuild()
    if not report.success:
        raise RuntimeError(f"Search index rebuild failed for {len(report.errors)} record(s)")

    server = JDConsoleServer((host, port), _RequestHandler)
    server.repository = repository
    server.search_index = search_index
    return server


class _RequestHandler(BaseHTTPRequestHandler):
    server: JDConsoleServer

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host_header():
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
        self._method_not_allowed()

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

        payload = stored.record.to_dict()
        payload.update(
            {
                "jd_markdown": stored.jd_markdown,
                "screening_markdown": stored.screening_markdown,
                "has_screening": stored.screening_markdown is not None,
            }
        )
        self._json(HTTPStatus.OK, payload)

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
            "default-src 'self'; object-src 'none'; base-uri 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _valid_host_header(self) -> bool:
        value = self.headers.get("Host", "")
        hostname = value.rsplit(":", 1)[0].strip("[]")
        return hostname == "localhost" or is_loopback(hostname)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
