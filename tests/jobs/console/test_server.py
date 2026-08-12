from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, cast
from types import SimpleNamespace

import pytest

from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.screening import build_fallback_output
from careerkit.jobs.console.server import create_server
from careerkit.jobs.domain.model import JobKey, JobRecord, ScreeningVerdict


def _pipeline_service(repository: JDRecordRepository, tmp_path: Path) -> JobsPipelineService:
    return JobsPipelineService(
        workspace_root=tmp_path,
        repository=repository,
        runtime_dir=tmp_path / "runtime",
    )


@contextmanager
def running_server(tmp_path: Path) -> Iterator[tuple[str, int]]:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/42",
            screening_verdict=ScreeningVerdict.RECOMMENDED,
        ),
        jd_markdown="# JD\n<script>alert(1)</script>",
    )
    repository.update_screening_result(
        repository.list()[0].record.key,
        screening_markdown="# Screening\nrecommended",
    )
    repository.create(
        JobRecord(
            platform="remember",
            job_id="42",
            company="Other",
            position="API Engineer",
            source_url="https://career.rememberapp.co.kr/job/posting/42",
        ),
        jd_markdown="# Other JD",
    )
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    host: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: list[tuple[str, str]] | dict[str, str] | None = None,
    skip_host: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    header_items = list(headers.items()) if isinstance(headers, dict) else list(headers or ())
    header_names = {key.lower() for key, _ in header_items}
    connection.putrequest(method, path, skip_host=skip_host or "host" in header_names)
    for key, value in header_items:
        connection.putheader(key, value)
    connection.endheaders(body)
    response = connection.getresponse()
    body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, body


def test_search_and_composite_detail_contract(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        status, headers, body = request(host, port, "/api/jobs?job_id=42")
        payload = json.loads(body)
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert [(item["platform"], item["job_id"]) for item in payload["items"]] == [
            ("remember", "42"),
            ("wanted", "42"),
        ]

        status, _, body = request(host, port, "/api/jobs/wanted/42")
        detail = json.loads(body)
        assert status == 200
        assert detail["jd_markdown"] == "# JD\n<script>alert(1)</script>"
        assert detail["screening_markdown"] == "# Screening\nrecommended"


def test_detail_returns_fallback_provider_for_published_fallback_document(tmp_path: Path) -> None:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    stored = repository.create(
        JobRecord(
            platform="wanted",
            job_id="77",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/77",
            screening_verdict=ScreeningVerdict.HOLD,
        ),
        jd_markdown="# JD\n",
    )
    repository.update_screening_result(
        stored.record.key,
        screening_markdown=build_fallback_output(stored, stored.jd_markdown, "assessment-contract-exhausted"),
        screening_provider="fallback",
    )
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = request("127.0.0.1", server.server_port, "/api/jobs/wanted/77")
        detail = json.loads(body)
        assert status == 200
        assert detail["screening_provider"] == "fallback"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_missing_screening_host_validation_and_write_rejection(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        status, _, body = request(host, port, "/api/jobs/remember/42")
        detail = json.loads(body)
        assert status == 200
        assert detail["screening_markdown"] is None
        assert detail["has_screening"] is False
        assert request(host, port, "/api/jobs", headers={"Host": "evil.example"})[0] == 403
        assert request(host, port, "/api/jobs", method="POST")[0] == 405


def test_static_allowlist_refresh_and_integrity_errors(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        status, headers, body = request(host, port, "/")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert b"JD Console" in body
        assert request(host, port, "/app.js")[0] == 200
        assert request(host, port, "/styles.css")[0] == 200
        theme_status, theme_headers, _ = request(host, port, "/theme.js")
        assert theme_status == 200
        assert theme_headers["content-type"].startswith("text/javascript")
        assert request(host, port, "/../../templates/jd/constants.py")[0] == 404

        repository = JDRecordRepository(tmp_path / "records")
        repository.create(
            JobRecord(
                platform="wanted",
                job_id="99",
                company="Fresh",
                position="Platform Engineer",
                source_url="https://www.wanted.co.kr/wd/99",
            ),
            jd_markdown="# Fresh JD",
        )
        assert json.loads(request(host, port, "/api/jobs?job_id=99")[2])["total"] == 0
        status, _, body = request(host, port, "/api/jobs?job_id=99&refresh=1")
        assert status == 200
        assert json.loads(body)["total"] == 1

        jd_path = next((tmp_path / "records" / "wanted" / "42" / "content").glob("*/jd.md"))
        jd_path.write_text("tampered", encoding="utf-8")
        detail_status, _, detail_body = request(host, port, "/api/jobs/wanted/42")
        refresh_status, _, refresh_body = request(host, port, "/api/jobs?refresh=1")
        assert detail_status == 500
        assert json.loads(detail_body) == {"error": "record integrity check failed"}
        assert refresh_status == 500
        assert json.loads(refresh_body) == {"error": "search index refresh failed", "error_count": 1}


def _patch_status(
    host: str,
    port: int,
    *,
    job_key: str = "wanted/42",
    payload: dict[str, object],
    origin: str | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers: list[tuple[str, str]] = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    if origin is not None:
        headers.append(("Origin", origin))
    if extra_headers is not None:
        headers.extend(extra_headers)
    status, response_headers, response_body = request(
        host,
        port,
        f"/api/jobs/{job_key}/application-status",
        method="PATCH",
        body=body,
        headers=headers,
    )
    return status, response_headers, cast(dict[str, Any], json.loads(response_body))


def _manifest_bytes(records_root: Path, key: JobKey) -> bytes:
    return (records_root / key.platform / key.job_id / "record.json").read_bytes()


def test_patch_updates_canonical_record_and_search_projection(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        status, _, payload = _patch_status(
            host,
            port,
            payload={
                "application_status": "interview",
                "occurred_at": "2026-08-10T09:30:00+09:00",
                "note": "1차 기술 면접",
            },
            origin=f"http://{host}:{port}",
        )

        assert status == 200
        history = payload["application_history"]
        assert isinstance(history, list)
        assert payload["application_status"] == "interview"
        assert payload["application_status_updated_at"] == "2026-08-10T09:30:00+09:00"
        assert history[-1] == {
            "status": "interview",
            "occurred_at": "2026-08-10T09:30:00+09:00",
            "note": "1차 기술 면접",
        }
        assert payload["index_refreshed"] is True
        assert payload["index_warning"] is None

        search_status, _, search_body = request(host, port, "/api/jobs?job_id=42")
        assert search_status == 200
        search_payload = json.loads(search_body)
        assert [(item["platform"], item["job_id"], item["application_status"]) for item in search_payload["items"]] == [
            ("remember", "42", "pending"),
            ("wanted", "42", "interview"),
        ]


def test_patch_rejects_origin_variants_before_body_validation_and_preserves_manifest(tmp_path: Path) -> None:
    records = tmp_path / "records"
    key = JobKey("wanted", "42")
    with running_server(tmp_path) as (host, port):
        before = _manifest_bytes(records, key)
        oversized_length = 10_000
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(oversized_length)),
        ]

        cases = [
            (None, [], "missing origin"),
            ("null", [], "null origin"),
            ("https://127.0.0.1:%d" % port, [], "wrong scheme"),
            ("http://localhost:%d" % port, [], "wrong host"),
            ("http://127.0.0.1:9999", [], "wrong port"),
            ("http://127.0.0.1:%d" % port, [("Origin", "http://127.0.0.1:%d" % port)], "duplicate origin"),
        ]
        for origin, extra_headers, _ in cases:
            merged = list(headers)
            if origin is not None:
                merged.append(("Origin", origin))
            merged.extend(extra_headers)
            status, _, body = request(
                host,
                port,
                "/api/jobs/wanted/42/application-status",
                method="PATCH",
                body=b"{}",
                headers=merged,
            )
            assert status == 403
            assert json.loads(body) == {"error": "invalid origin"}
            assert _manifest_bytes(records, key) == before


def test_patch_rejects_invalid_json_shape_without_mutation(tmp_path: Path) -> None:
    records = tmp_path / "records"
    key = JobKey("wanted", "42")
    with running_server(tmp_path) as (host, port):
        before = _manifest_bytes(records, key)

        status, _, payload = _patch_status(
            host,
            port,
            payload={"application_status": "offer", "extra": "boom"},
            origin=f"http://{host}:{port}",
        )
        assert status == 400
        assert payload == {"error": "request body must contain only application_status, occurred_at, and note"}
        assert _manifest_bytes(records, key) == before

        status, _, payload = _patch_status(
            host,
            port,
            payload={"note": "메모만 저장"},
            origin=f"http://{host}:{port}",
        )
        assert status == 400
        assert payload == {"error": "application note requires application_status"}
        assert _manifest_bytes(records, key) == before

        status, _, payload = _patch_status(
            host,
            port,
            payload={"application_status": None},
            origin=f"http://{host}:{port}",
        )
        assert status == 400
        assert payload == {"error": "application_status is required"}
        assert _manifest_bytes(records, key) == before


def test_patch_rejection_matrix_preserves_manifest_bytes(tmp_path: Path) -> None:
    records = tmp_path / "records"
    key = JobKey("wanted", "42")
    valid_body = json.dumps({"application_status": "applied"}).encode("utf-8")
    with running_server(tmp_path) as (host, port):
        origin = f"http://{host}:{port}"
        valid_headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(valid_body))),
            ("Origin", origin),
        ]
        cases = [
            (
                "missing host",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                valid_headers,
                True,
                403,
                "invalid host",
            ),
            (
                "invalid host",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Host", "evil.example"), *valid_headers],
                False,
                403,
                "invalid host",
            ),
            (
                "duplicate host",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Host", f"{host}:{port}"), ("Host", f"{host}:{port}"), *valid_headers],
                False,
                403,
                "invalid host",
            ),
            (
                "missing content type",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Content-Length", str(len(valid_body))), ("Origin", origin)],
                False,
                400,
                "content type must be application/json",
            ),
            (
                "duplicate content type",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Content-Type", "application/json"), *valid_headers],
                False,
                400,
                "content type must be application/json",
            ),
            (
                "wrong content type",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [
                    ("Content-Type", "text/plain"),
                    ("Content-Length", str(len(valid_body))),
                    ("Origin", origin),
                ],
                False,
                400,
                "content type must be application/json",
            ),
            (
                "missing content length",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Content-Type", "application/json"), ("Origin", origin)],
                False,
                400,
                "content length is required",
            ),
            (
                "duplicate content length",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [("Content-Length", str(len(valid_body))), *valid_headers],
                False,
                400,
                "content length is required",
            ),
            (
                "noninteger content length",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "abc"),
                    ("Origin", origin),
                ],
                False,
                400,
                "content length must be an integer",
            ),
            (
                "negative content length",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "-1"),
                    ("Origin", origin),
                ],
                False,
                400,
                "content length must not be negative",
            ),
            (
                "oversized content length",
                "/api/jobs/wanted/42/application-status",
                valid_body,
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "8193"),
                    ("Origin", origin),
                ],
                False,
                400,
                "request body must be 8192 bytes or fewer",
            ),
            (
                "malformed json",
                "/api/jobs/wanted/42/application-status",
                b"{broken",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "7"),
                    ("Origin", origin),
                ],
                False,
                400,
                "request body must be valid JSON",
            ),
            (
                "invalid utf8",
                "/api/jobs/wanted/42/application-status",
                b"\xff",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "1"),
                    ("Origin", origin),
                ],
                False,
                400,
                "request body must be valid JSON",
            ),
            (
                "non-object json",
                "/api/jobs/wanted/42/application-status",
                b"[]",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", "2"),
                    ("Origin", origin),
                ],
                False,
                400,
                "request body must be a JSON object",
            ),
        ]
        payload_cases = [
            ("unknown field", {"application_status": "applied", "extra": True}, "request body must contain only application_status, occurred_at, and note"),
            ("missing status", {}, "application_status is required"),
            ("null status", {"application_status": None}, "application_status is required"),
            ("timestamp only", {"occurred_at": "2026-08-10T09:30:00+09:00"}, "occurred_at requires application_status"),
            ("note only", {"note": "메모"}, "application note requires application_status"),
            ("non-string status", {"application_status": 1}, "application_status must be a string when provided"),
            ("non-string timestamp", {"application_status": "applied", "occurred_at": 1}, "occurred_at must be a string when provided"),
            ("non-string note", {"application_status": "applied", "note": 1}, "note must be a string when provided"),
            ("invalid status", {"application_status": "withdrawn"}, "invalid application_status"),
            ("invalid timestamp", {"application_status": "applied", "occurred_at": "not-a-time"}, "invalid ISO 8601 timestamp: not-a-time"),
            ("oversized note", {"application_status": "applied", "note": "x" * 2001}, "note must be 2000 characters or fewer"),
        ]
        for name, payload, error in payload_cases:
            body = json.dumps(payload).encode("utf-8")
            cases.append(
                (
                    name,
                    "/api/jobs/wanted/42/application-status",
                    body,
                    [
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(body))),
                        ("Origin", origin),
                    ],
                    False,
                    400,
                    error,
                )
            )
        cases.extend(
            [
                (
                    "invalid route key",
                    "/api/jobs/%20/42/application-status",
                    valid_body,
                    valid_headers,
                    False,
                    400,
                    "Invalid platform: ' '",
                ),
                (
                    "missing record",
                    "/api/jobs/wanted/404/application-status",
                    valid_body,
                    valid_headers,
                    False,
                    404,
                    "record not found",
                ),
            ]
        )

        for name, path, body, headers, skip_host, expected_status, expected_error in cases:
            before = _manifest_bytes(records, key)
            status, _, response = request(
                host,
                port,
                path,
                method="PATCH",
                body=body,
                headers=headers,
                skip_host=skip_host,
            )

            assert status == expected_status, name
            assert json.loads(response) == {"error": expected_error}, name
            assert _manifest_bytes(records, key) == before, name


def test_patch_rejects_non_status_routes_without_reading_or_mutating_manifest(tmp_path: Path) -> None:
    records = tmp_path / "records"
    key = JobKey("wanted", "42")
    with running_server(tmp_path) as (host, port):
        before = _manifest_bytes(records, key)
        body = json.dumps({"application_status": "offer"}).encode("utf-8")
        status, _, response = request(
            host,
            port,
            "/api/jobs/wanted/42",
            method="PATCH",
            body=body,
            headers=[
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Origin", f"http://{host}:{port}"),
            ],
        )

        assert status == 405
        assert response == b""
        assert _manifest_bytes(records, key) == before


def test_patch_reports_index_warning_after_canonical_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/42",
        ),
        jd_markdown="# JD",
    )
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )
    def fake_rebuild() -> object:
        return SimpleNamespace(success=False, errors=(SimpleNamespace(message="sqlite busy"),), indexed_count=0)

    monkeypatch.setattr(server.search_index, "rebuild", fake_rebuild)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, payload = _patch_status(
            "127.0.0.1",
            server.server_port,
            payload={"application_status": "applied", "note": "지원서 제출"},
            origin=f"http://127.0.0.1:{server.server_port}",
        )
        assert status == 200
        assert payload["index_refreshed"] is False
        assert payload["index_warning"] == "canonical update saved; search index refresh failed"
        stored = repository.get(JobKey("wanted", "42"))
        assert stored.record.application_history[-1].note == "지원서 제출"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_patch_reports_index_warning_when_rebuild_raises_expected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/42",
        ),
        jd_markdown="# JD",
    )
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )

    def raise_rebuild() -> object:
        raise OSError("sqlite busy")

    monkeypatch.setattr(server.search_index, "rebuild", raise_rebuild)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, payload = _patch_status(
            "127.0.0.1",
            server.server_port,
            payload={"application_status": "applied", "note": "지원서 제출"},
            origin=f"http://127.0.0.1:{server.server_port}",
        )
        assert status == 200
        assert payload["index_refreshed"] is False
        assert payload["index_warning"] == "canonical update saved; search index refresh failed"
        stored = repository.get(JobKey("wanted", "42"))
        assert stored.record.application_history[-1].note == "지원서 제출"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_patch_reports_index_warning_when_rebuild_raises_sqlite_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/42",
        ),
        jd_markdown="# JD",
    )
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )

    def raise_rebuild() -> object:
        raise sqlite3.OperationalError("sqlite busy")

    monkeypatch.setattr(server.search_index, "rebuild", raise_rebuild)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, payload = _patch_status(
            "127.0.0.1",
            server.server_port,
            payload={"application_status": "applied", "note": "지원서 제출"},
            origin=f"http://127.0.0.1:{server.server_port}",
        )
        assert status == 200
        assert payload["index_refreshed"] is False
        assert payload["index_warning"] == "canonical update saved; search index refresh failed"
        stored = repository.get(JobKey("wanted", "42"))
        assert stored.record.application_history[-1].note == "지원서 제출"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_console_responses_set_frame_ancestors_none(tmp_path: Path) -> None:
    with running_server(tmp_path) as (host, port):
        status, headers, _ = request(host, port, "/")
        assert status == 200
        assert headers["content-security-policy"] == (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )


def test_create_server_rejects_pipeline_repository_root_mismatch(tmp_path: Path) -> None:
    records = tmp_path / "records-a"
    other_records = tmp_path / "records-b"
    repository = JDRecordRepository(other_records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
        ),
        jd_markdown="# JD",
    )

    with pytest.raises(ValueError, match="pipeline repository root must match records_root"):
        create_server(
            records_root=records,
            database_path=tmp_path / "derived" / "search.sqlite3",
            pipeline_service=_pipeline_service(repository, tmp_path),
            host="127.0.0.1",
            port=0,
        )
