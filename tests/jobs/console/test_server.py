from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import threading
from typing import Iterator

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.console.server import create_server
from careerkit.jobs.domain.model import JobRecord, ScreeningVerdict


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


def request(host: str, port: int, path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(host, port, timeout=2)
    connection.request(method, path, headers=headers or {})
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
