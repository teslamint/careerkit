"""Unit tests for the careerkit Native Messaging host."""

from __future__ import annotations

import importlib.util
import io
import json
import queue
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from careerkit.jobs.adapters.storage.file_records import JobRecordNotFound, StoredJobRecord
from careerkit.jobs.domain.model import JobKey, JobRecord, ScreeningVerdict

_HOST_PATH = Path(__file__).parents[2] / "ext" / "native-host" / "careerkit_host.py"


def _load_host_module() -> Any:
    spec = importlib.util.spec_from_file_location("careerkit_host", _HOST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = _load_host_module()


def _make_record(**overrides) -> JobRecord:
    defaults: dict[str, Any] = dict(
        platform="wanted",
        job_id="123456",
        company="Acme",
        position="Backend Engineer",
        source_url="https://www.wanted.co.kr/wd/123456",
        screening_verdict=ScreeningVerdict.HOLD,
        verdict_capped=False,
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def test_write_then_read_message_round_trips():
    stream = io.BytesIO()
    host.write_message(stream, {"status": "ok", "data": {"a": 1}})
    stream.seek(0)

    assert host.read_message(stream) == {"status": "ok", "data": {"a": 1}}


def test_read_message_parses_4byte_little_endian_length_prefix():
    payload = json.dumps({"action": "ping"}).encode("utf-8")
    stream = io.BytesIO(struct.pack("<I", len(payload)) + payload)

    assert host.read_message(stream) == {"action": "ping"}


def test_write_message_emits_4byte_little_endian_length_prefix():
    stream = io.BytesIO()
    host.write_message(stream, {"action": "ping"})
    raw = stream.getvalue()
    (length,) = struct.unpack("<I", raw[:4])

    assert length == len(raw) - 4
    assert json.loads(raw[4:]) == {"action": "ping"}


def test_read_message_returns_none_on_clean_eof():
    assert host.read_message(io.BytesIO(b"")) is None


def test_handle_ping_returns_ok():
    repository = MagicMock()

    assert host.handle_ping({"action": "ping"}, repository) == {"status": "ok"}


def test_handle_lookup_existing_record_returns_verdict_data():
    record = _make_record()
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    response = host.handle_lookup(
        {"action": "lookup", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["status"] == "ok"
    assert response["data"]["screening_verdict"] == "hold"
    assert response["data"]["verdict_capped"] is False
    repository.find.assert_called_once_with(JobKey("wanted", "123456"))


def test_handle_lookup_missing_record_returns_null_data():
    repository = MagicMock()
    repository.find.return_value = None

    response = host.handle_lookup(
        {"action": "lookup", "url": "https://www.wanted.co.kr/wd/999999"}, repository
    )

    assert response == {"status": "ok", "data": None}


def test_handle_lookup_unrecognized_url_returns_null_without_repository_call():
    repository = MagicMock()

    response = host.handle_lookup({"action": "lookup", "url": "https://example.com/not-a-job"}, repository)

    assert response == {"status": "ok", "data": None}
    repository.find.assert_not_called()


def test_resolve_key_rejects_http_scheme():
    repository = MagicMock()
    response = host.handle_lookup({"action": "lookup", "url": "http://www.wanted.co.kr/wd/123"}, repository)
    assert response == {"status": "ok", "data": None}
    repository.find.assert_not_called()


def test_resolve_key_rejects_ssrf_url_with_platform_in_query():
    repository = MagicMock()
    response = host.handle_lookup(
        {"action": "lookup", "url": "https://evil.example/?x=wanted.co.kr/wd/123"}, repository
    )
    assert response == {"status": "ok", "data": None}
    repository.find.assert_not_called()


def test_resolve_key_rejects_unknown_host():
    repository = MagicMock()
    response = host.handle_lookup(
        {"action": "lookup", "url": "https://attacker.wanted.co.kr/wd/123"}, repository
    )
    assert response == {"status": "ok", "data": None}
    repository.find.assert_not_called()


def test_handle_get_detail_returns_full_record():
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(
        record=record, jd_markdown="# JD body", screening_markdown="# Screening body"
    )

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["status"] == "ok"
    assert response["data"]["jd_markdown"] == "# JD body"
    assert response["data"]["screening_markdown"] == "# Screening body"
    assert response["data"]["record"]["job_id"] == "123456"
    repository.get.assert_called_once_with(JobKey("wanted", "123456"))


def test_handle_get_detail_missing_record_returns_null_data():
    repository = MagicMock()
    repository.get.side_effect = JobRecordNotFound("nope")

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response == {"status": "ok", "data": None}


def test_handle_get_detail_omits_jd_markdown_when_over_size_limit():
    record = _make_record()
    repository = MagicMock()
    huge_jd = "x" * 950_000
    repository.get.return_value = StoredJobRecord(
        record=record, jd_markdown=huge_jd, screening_markdown="short"
    )

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["data"]["jd_markdown"] is None
    assert response["data"]["truncated"] is True
    assert response["data"]["screening_markdown"] == "short"


def test_dispatch_unknown_action_returns_error():
    repository = MagicMock()

    response, work_item = host.dispatch({"action": "not_a_real_action"}, repository)

    assert response["status"] == "error"
    assert "not_a_real_action" in response["message"]
    assert work_item is None


def test_read_message_rejects_oversized_message():
    length = host._MAX_MESSAGE_BYTES + 1
    body = b"x" * length
    stream = io.BytesIO(struct.pack("<I", length) + body)

    with pytest.raises(ValueError, match="message too large"):
        host.read_message(stream)


def test_read_message_drains_oversized_body_and_reads_next():
    oversized_length = host._MAX_MESSAGE_BYTES + 100
    oversized_body = b"x" * oversized_length
    good_payload = json.dumps({"action": "ping"}).encode("utf-8")
    stream = io.BytesIO(
        struct.pack("<I", oversized_length)
        + oversized_body
        + struct.pack("<I", len(good_payload))
        + good_payload
    )

    with pytest.raises(ValueError, match="message too large"):
        host.read_message(stream)

    assert host.read_message(stream) == {"action": "ping"}


def test_read_message_at_size_limit_passes():
    payload = b"x" * host._MAX_MESSAGE_BYTES
    stream = io.BytesIO(struct.pack("<I", len(payload)) + payload)

    with pytest.raises(json.JSONDecodeError):
        host.read_message(stream)


def test_serve_echoes_request_id_in_response():
    ping_payload = json.dumps({"action": "ping", "request_id": 42}).encode("utf-8")
    stdin = io.BytesIO(struct.pack("<I", len(ping_payload)) + ping_payload)
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(stdin, stdout, repository)

    stdout.seek(0)
    response = host.read_message(stdout)
    assert response == {"status": "ok", "request_id": 42}


def test_serve_echoes_request_id_in_error_response():
    payload = json.dumps({"action": "not_real", "request_id": 99}).encode("utf-8")
    stdin = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(stdin, stdout, repository)

    stdout.seek(0)
    response = host.read_message(stdout)
    assert response["status"] == "error"
    assert response["request_id"] == 99


def test_serve_omits_request_id_when_not_in_request():
    payload = json.dumps({"action": "ping"}).encode("utf-8")
    stdin = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(stdin, stdout, repository)

    stdout.seek(0)
    response = host.read_message(stdout)
    assert response == {"status": "ok"}
    assert "request_id" not in response


def test_serve_no_request_id_on_parse_error():
    bad_payload = b"{not valid json"
    stdin = io.BytesIO(struct.pack("<I", len(bad_payload)) + bad_payload)
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(stdin, stdout, repository)

    stdout.seek(0)
    response = host.read_message(stdout)
    assert response["status"] == "error"
    assert "request_id" not in response


def test_serve_returns_error_response_on_malformed_json():
    bad_payload = b"{not valid json"
    ping_payload = json.dumps({"action": "ping"}).encode("utf-8")
    stdin = io.BytesIO(
        struct.pack("<I", len(bad_payload))
        + bad_payload
        + struct.pack("<I", len(ping_payload))
        + ping_payload
    )
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(stdin, stdout, repository)

    stdout.seek(0)
    first = host.read_message(stdout)
    second = host.read_message(stdout)

    assert first["status"] == "error"
    assert second == {"status": "ok"}


def test_serve_stops_cleanly_on_empty_stdin():
    stdout = io.BytesIO()
    repository = MagicMock()

    host.serve(io.BytesIO(b""), stdout, repository)

    assert stdout.getvalue() == b""


def test_serve_writes_accepted_response_before_enqueuing_collect_work():
    payload = json.dumps({"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}).encode("utf-8")
    stdin = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    stdout = io.BytesIO()
    repository = MagicMock()
    repository.find.return_value = None

    class _QueueThatPushesOnPut:
        def __init__(self, stream: io.BytesIO) -> None:
            self.stream = stream
            self.items: list[tuple[str, str, bool]] = []

        def put(self, item: tuple[str, str, bool]) -> None:
            self.items.append(item)
            host.write_message(
                self.stream,
                {
                    "type": "screening_progress",
                    "tracking_id": item[1],
                    "stage": "company_info",
                    "state": "checking",
                },
            )

        def join(self) -> None:
            return None

    work_queue = _QueueThatPushesOnPut(stdout)

    host.serve(stdin, stdout, repository, work_queue=work_queue)

    stdout.seek(0)
    accepted = host.read_message(stdout)
    progress = host.read_message(stdout)
    assert accepted["status"] == "accepted"
    assert progress["type"] == "screening_progress"
    assert accepted["tracking_id"] == progress["tracking_id"]


def test_serve_writes_accepted_response_before_enqueuing_rescreen_work():
    payload = json.dumps({"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"}).encode("utf-8")
    stdin = io.BytesIO(struct.pack("<I", len(payload)) + payload)
    stdout = io.BytesIO()
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=_make_record(), jd_markdown="# JD", screening_markdown="# S")

    class _QueueThatPushesOnPut:
        def __init__(self, stream: io.BytesIO) -> None:
            self.stream = stream
            self.items: list[tuple[str, str, bool]] = []

        def put(self, item: tuple[str, str, bool]) -> None:
            self.items.append(item)
            host.write_message(
                self.stream,
                {
                    "type": "screening_progress",
                    "tracking_id": item[1],
                    "stage": "company_info",
                    "state": "checking",
                },
            )

        def join(self) -> None:
            return None

    work_queue = _QueueThatPushesOnPut(stdout)

    host.serve(stdin, stdout, repository, work_queue=work_queue)

    stdout.seek(0)
    accepted = host.read_message(stdout)
    progress = host.read_message(stdout)
    assert accepted["status"] == "accepted"
    assert accepted["action"] == "rescreen"
    assert progress["type"] == "screening_progress"
    assert accepted["tracking_id"] == progress["tracking_id"]


def test_handle_collect_duplicate_returns_existing_record():
    record = _make_record(screening_verdict=ScreeningVerdict.RECOMMENDED)
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown="# S")
    work_queue = queue.Queue()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    # Shape fixed by panel.js (`response.status === "duplicate"`) and badge.js
    # (renders `response.data` as a record dict, same as lookup's response).
    assert response["status"] == "duplicate"
    assert response["data"] == record.to_dict()
    assert work_queue.empty()
    repository.find.assert_called_once_with(JobKey("wanted", "123456"))


def test_handle_collect_prescreened_record_returns_duplicate():
    """A set-aside record carries a reason instead of a verdict. It is already
    decided, so re-collecting it must not open a second, unbounded door to full
    screening — `queue prescreened --screen --limit` owns that path."""
    record = _make_record(screening_verdict=None, prescreen_reason="title_exclude")
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    work_queue = queue.Queue()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    assert response["status"] == "duplicate"
    assert response["data"] == record.to_dict()
    assert response["data"]["prescreen_reason"] == "title_exclude"
    assert work_queue.empty()


def test_handle_collect_record_without_verdict_or_reason_still_screens():
    """Neither a verdict nor a reason means screening never happened. That record
    still enqueues with screening_only=True."""
    record = _make_record(screening_verdict=None, prescreen_reason=None)
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    work_queue = queue.Queue()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    assert response["status"] == "accepted"
    queued_url, queued_tracking_id, queued_screening_only = work_queue.get_nowait()
    assert queued_url == "https://www.wanted.co.kr/wd/123456"
    assert queued_tracking_id == response["tracking_id"]
    assert queued_screening_only is True


def test_handle_collect_new_url_returns_accepted_and_enqueues():
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    assert response["status"] == "accepted"
    assert response["action"] == "collect"
    # tracking_id is top-level: service-worker.js reads `response.tracking_id`.
    tracking_id = response["tracking_id"]
    assert tracking_id

    queued_url, queued_tracking_id, queued_screening_only = work_queue.get_nowait()
    assert queued_url == "https://www.wanted.co.kr/wd/123456"
    assert queued_tracking_id == tracking_id
    assert queued_screening_only is False


def test_handle_collect_unrecognized_url_returns_error():
    repository = MagicMock()
    work_queue = queue.Queue()

    response = host.handle_collect({"action": "collect", "url": "https://example.com/nope"}, repository, work_queue)

    assert response["status"] == "error"
    assert work_queue.empty()
    repository.find.assert_not_called()


def test_dispatch_collect_without_queue_returns_error():
    repository = MagicMock()

    response, work_item = host.dispatch({"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository)

    assert response["status"] == "error"
    assert work_item is None
    repository.find.assert_not_called()


def test_run_screening_worker_writes_ordered_progress_and_sanitized_completion_on_success():
    record = _make_record(screening_verdict=ScreeningVerdict.RECOMMENDED, verdict_capped=False)
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown="# S")

    extraction_batch = MagicMock()
    extraction_batch.records = [stored]
    extraction_batch.company_contexts = {"wanted:123456": object()}
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_result = MagicMock()
    screening_result.metadata = {
        "failures": [],
        "company_info_results": {
            "wanted:123456": {
                "status": "ready",
                "attempted": True,
                "persisted": True,
                "completeness": 100.0,
                "warning_code": None,
                "file_path": "/Users/private/company_info/acme.md",
                "source_url": "https://example.com/private",
            }
        },
    }
    screening_stage = MagicMock()
    screening_stage.screen.return_value = screening_result

    repository = MagicMock()
    repository.find.return_value = stored

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/123456", "track-1", False))

    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    # Run the worker body directly for one item instead of looping forever.
    with patch.object(
        work_queue,
        "get",
        side_effect=[("https://www.wanted.co.kr/wd/123456", "track-1", False), KeyboardInterrupt],
    ):
        try:
            host.run_screening_worker(work_queue, repository, extraction_stage, screening_stage, stdout, stdout_lock)
        except KeyboardInterrupt:
            pass

    extraction_stage.extract.assert_called_once_with(
        ["https://www.wanted.co.kr/wd/123456"], dry_run=False, screening_only=False
    )
    screening_stage.screen.assert_called_once_with(extraction_batch, dry_run=False, llm_timeout=180)

    stdout.seek(0)
    checking = host.read_message(stdout)
    enriching = host.read_message(stdout)
    running = host.read_message(stdout)
    push = host.read_message(stdout)
    assert checking == {
        "type": "screening_progress",
        "tracking_id": "track-1",
        "stage": "company_info",
        "state": "checking",
    }
    assert enriching == {
        "type": "screening_progress",
        "tracking_id": "track-1",
        "stage": "company_info",
        "state": "enriching",
    }
    assert running == {
        "type": "screening_progress",
        "tracking_id": "track-1",
        "stage": "screening",
        "state": "running",
    }
    # data is a full record dict (same shape as lookup/get_detail) plus
    # verdict_label, since badge.js renders it via renderFromRecord(data).
    expected_data = record.to_dict()
    expected_data["verdict_label"] = "지원 추천"
    expected_data["company_info"] = {
        "status": "ready",
        "attempted": True,
        "persisted": True,
        "completeness": 100.0,
        "warning_code": None,
    }
    assert push == {
        "type": "screening_complete",
        "tracking_id": "track-1",
        "data": expected_data,
    }


def test_run_screening_worker_writes_screening_failed_on_extraction_failure():
    extraction_batch = MagicMock()
    extraction_batch.records = []
    extraction_batch.metadata = {
        "failures": [{"item_id": "wanted:123456", "error": "/Users/teslamint/private/acme.md exploded"}]
    }
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_stage = MagicMock()
    repository = MagicMock()

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/123456", "track-2", False))
    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    with patch.object(
        work_queue,
        "get",
        side_effect=[("https://www.wanted.co.kr/wd/123456", "track-2", False), KeyboardInterrupt],
    ):
        try:
            host.run_screening_worker(work_queue, repository, extraction_stage, screening_stage, stdout, stdout_lock)
        except KeyboardInterrupt:
            pass

    screening_stage.screen.assert_not_called()
    stdout.seek(0)
    push = host.read_message(stdout)
    # message is top-level: service-worker.js's onScreeningFailed reads `message.message`.
    assert push == {
        "type": "screening_failed",
        "tracking_id": "track-2",
        "message": "job extraction failed",
    }


def test_run_screening_worker_sends_failed_when_screening_has_failures():
    """When screening stage skips a record (e.g. company info missing), the
    worker must send screening_failed, not screening_complete with null verdict."""
    record = _make_record(screening_verdict=None)
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    extraction_batch = MagicMock()
    extraction_batch.records = [stored]
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_result = MagicMock()
    screening_result.metadata = {
        "failures": [
            {
                "job_key": "wanted:123456",
                "error_code": "company_info_failed",
                "error": "/Users/teslamint/private/company_info/acme.md lock timeout: secret token",
            }
        ],
        "company_info_results": {},
    }
    screening_stage = MagicMock()
    screening_stage.screen.return_value = screening_result

    repository = MagicMock()
    repository.find.return_value = stored

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/123456", "track-3", False))
    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    with patch.object(
        work_queue,
        "get",
        side_effect=[("https://www.wanted.co.kr/wd/123456", "track-3", False), KeyboardInterrupt],
    ):
        try:
            host.run_screening_worker(work_queue, repository, extraction_stage, screening_stage, stdout, stdout_lock)
        except KeyboardInterrupt:
            pass

    stdout.seek(0)
    host.read_message(stdout)
    host.read_message(stdout)
    push = host.read_message(stdout)
    assert push == {
        "type": "screening_failed",
        "tracking_id": "track-3",
        "message": "company info unavailable",
    }


def test_handle_collect_rejects_duplicate_in_flight_url():
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()
    in_flight = {"https://www.wanted.co.kr/wd/123456"}
    in_flight_lock = threading.Lock()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, work_queue, in_flight, in_flight_lock,
    )

    assert response["status"] == "error"
    assert "already queued" in response["message"]
    assert work_queue.empty()


def test_handle_collect_rejects_when_queue_full():
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()
    in_flight = {f"https://www.wanted.co.kr/wd/{i}" for i in range(10)}
    in_flight_lock = threading.Lock()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/999999"},
        repository, work_queue, in_flight, in_flight_lock,
    )

    assert response["status"] == "error"
    assert "queue full" in response["message"]
    assert work_queue.empty()


def test_handle_collect_adds_url_to_in_flight():
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()
    in_flight = set()
    in_flight_lock = threading.Lock()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, work_queue, in_flight, in_flight_lock,
    )

    assert response["status"] == "accepted"
    assert "https://www.wanted.co.kr/wd/123456" in in_flight


def test_handle_collect_without_in_flight_still_works():
    """Backwards compat: in_flight=None skips dedup/cap checks."""
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()

    response = host.handle_collect(
        {"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, work_queue,
    )

    assert response["status"] == "accepted"


def test_ensure_worker_alive_restarts_dead_worker():
    """When the worker thread dies, _ensure_worker_alive drains queue, sends
    screening_failed for orphaned items, clears in_flight, and restarts."""
    dead_worker = MagicMock()
    dead_worker.is_alive.return_value = False
    in_flight = {"https://www.wanted.co.kr/wd/111"}
    in_flight_lock = threading.Lock()
    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/222", "orphan-1", False))
    work_queue.put(("https://www.wanted.co.kr/wd/333", "orphan-2", True))

    host._worker_context = {
        "worker": dead_worker,
        "worker_args": (work_queue, MagicMock(), MagicMock(), MagicMock(), stdout, stdout_lock, in_flight, in_flight_lock),
        "in_flight": in_flight,
        "in_flight_lock": in_flight_lock,
    }
    try:
        host._ensure_worker_alive()

        new_worker = host._worker_context["worker"]
        assert new_worker is not dead_worker
        assert new_worker.is_alive()
        assert len(in_flight) == 0
        assert work_queue.empty()

        stdout.seek(0)
        push1 = host.read_message(stdout)
        push2 = host.read_message(stdout)
        assert push1 == {"type": "screening_failed", "tracking_id": "orphan-1", "message": "worker restarted"}
        assert push2 == {"type": "screening_failed", "tracking_id": "orphan-2", "message": "worker restarted"}
    finally:
        host._worker_context = None


def test_ensure_worker_alive_noop_when_alive():
    """When the worker is alive, _ensure_worker_alive does nothing."""
    alive_worker = MagicMock()
    alive_worker.is_alive.return_value = True

    host._worker_context = {
        "worker": alive_worker,
        "worker_args": (queue.Queue(), MagicMock(), MagicMock(), MagicMock(), io.BytesIO(), threading.Lock()),
    }
    try:
        host._ensure_worker_alive()
        assert host._worker_context["worker"] is alive_worker
    finally:
        host._worker_context = None


def test_run_screening_worker_sends_failed_when_verdict_is_null():
    """When screening completes without setting a verdict (no failure recorded
    but verdict still null), the worker must send screening_failed."""
    record = _make_record(screening_verdict=None)
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    extraction_batch = MagicMock()
    extraction_batch.records = [stored]
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_result = MagicMock()
    screening_result.metadata = {"failures": [], "item_ids": []}
    screening_stage = MagicMock()
    screening_stage.screen.return_value = screening_result

    repository = MagicMock()
    repository.find.return_value = stored

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/123456", "track-4", False))
    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    with patch.object(
        work_queue,
        "get",
        side_effect=[("https://www.wanted.co.kr/wd/123456", "track-4", False), KeyboardInterrupt],
    ):
        try:
            host.run_screening_worker(work_queue, repository, extraction_stage, screening_stage, stdout, stdout_lock)
        except KeyboardInterrupt:
            pass

    stdout.seek(0)
    host.read_message(stdout)
    host.read_message(stdout)
    push = host.read_message(stdout)
    assert push == {
        "type": "screening_failed",
        "tracking_id": "track-4",
        "message": "screening failed",
    }


def test_run_screening_worker_sends_complete_with_set_aside_label_when_prescreen_reason_is_set():
    """A pre-screen skip is a recorded state, not a failure. When screening leaves
    a reason instead of a verdict, the worker emits screening_complete carrying the
    set-aside label — and the company_info the service worker validates on."""
    record = _make_record(screening_verdict=None, prescreen_reason="title_exclude")
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    extraction_batch = MagicMock()
    extraction_batch.records = [stored]
    extraction_batch.company_contexts = {}
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_result = MagicMock()
    screening_result.metadata = {
        "failures": [],
        "item_ids": [],
        "prescreened_count": 1,
        "prescreen_reasons": {"title_exclude": 1},
        # Populated before the pre-screen `continue` in JobsScreeningStage.screen,
        # so a set-aside item still has one. isValidCompletionMessage in
        # service-worker.js drops any screening_complete lacking it.
        "company_info_results": {
            "wanted:123456": {
                "status": "ready",
                "attempted": True,
                "persisted": True,
                "completeness": 100.0,
                "warning_code": None,
                "file_path": "/Users/private/company_info/acme.md",
            }
        },
    }
    screening_stage = MagicMock()
    screening_stage.screen.return_value = screening_result

    repository = MagicMock()
    repository.find.return_value = stored

    work_queue = queue.Queue()
    work_queue.put(("https://www.wanted.co.kr/wd/123456", "track-5", False))
    stdout = io.BytesIO()
    stdout_lock = threading.Lock()

    with patch.object(
        work_queue,
        "get",
        side_effect=[("https://www.wanted.co.kr/wd/123456", "track-5", False), KeyboardInterrupt],
    ):
        try:
            host.run_screening_worker(work_queue, repository, extraction_stage, screening_stage, stdout, stdout_lock)
        except KeyboardInterrupt:
            pass

    stdout.seek(0)
    host.read_message(stdout)
    host.read_message(stdout)
    push = host.read_message(stdout)

    expected_data = record.to_dict()
    expected_data["verdict_label"] = "사전 필터 제외 기록"
    expected_data["company_info"] = {
        "status": "ready",
        "attempted": True,
        "persisted": True,
        "completeness": 100.0,
        "warning_code": None,
    }
    assert push == {
        "type": "screening_complete",
        "tracking_id": "track-5",
        "data": expected_data,
    }


# --- handle_rescreen ---


def test_handle_rescreen_existing_record_returns_accepted_and_enqueues():
    record = _make_record(screening_verdict=ScreeningVerdict.HOLD)
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown="# S")
    work_queue = queue.Queue()

    response = host.handle_rescreen(
        {"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    assert response["status"] == "accepted"
    assert response["action"] == "rescreen"
    tracking_id = response["tracking_id"]
    assert tracking_id

    queued_url, queued_tracking_id, queued_screening_only = work_queue.get_nowait()
    assert queued_url == "https://www.wanted.co.kr/wd/123456"
    assert queued_tracking_id == tracking_id
    assert queued_screening_only is True


def test_handle_rescreen_missing_record_returns_error():
    repository = MagicMock()
    repository.find.return_value = None
    work_queue = queue.Queue()

    response = host.handle_rescreen(
        {"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"}, repository, work_queue
    )

    assert response["status"] == "error"
    assert "collect first" in response["message"]
    assert work_queue.empty()


def test_handle_rescreen_unrecognized_url_returns_error():
    repository = MagicMock()
    work_queue = queue.Queue()

    response = host.handle_rescreen(
        {"action": "rescreen", "url": "https://example.com/nope"}, repository, work_queue
    )

    assert response["status"] == "error"
    assert work_queue.empty()
    repository.find.assert_not_called()


def test_handle_rescreen_rejects_duplicate_in_flight():
    record = _make_record()
    repository = MagicMock()
    repository.find.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown="# S")
    work_queue = queue.Queue()
    in_flight = {"https://www.wanted.co.kr/wd/123456"}
    in_flight_lock = threading.Lock()

    response = host.handle_rescreen(
        {"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, work_queue, in_flight, in_flight_lock,
    )

    assert response["status"] == "error"
    assert "already queued" in response["message"]
    assert work_queue.empty()


def test_dispatch_rescreen_without_queue_returns_error():
    repository = MagicMock()

    response, work_item = host.dispatch({"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"}, repository)

    assert response["status"] == "error"
    assert work_item is None
    repository.find.assert_not_called()


# --- get_detail is_fallback ---


def test_handle_get_detail_returns_is_fallback_true_for_fallback_document():
    from careerkit.jobs.application.screening import build_fallback_output
    jd_record = _make_record(screening_verdict=ScreeningVerdict.HOLD)
    stored_jd = StoredJobRecord(record=jd_record, jd_markdown="# JD content", screening_markdown=None)
    fallback_md = build_fallback_output(stored_jd, "# JD content", "llm timeout")
    record = _make_record(screening_verdict=ScreeningVerdict.HOLD)
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(
        record=record, jd_markdown="# JD", screening_markdown=fallback_md,
    )

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["data"]["is_fallback"] is True


def test_handle_get_detail_returns_is_fallback_false_for_normal_screening():
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(
        record=record, jd_markdown="# JD", screening_markdown="# Normal screening\n\nSome analysis.",
    )

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["data"]["is_fallback"] is False


def test_handle_get_detail_returns_is_fallback_false_when_no_screening():
    record = _make_record(screening_verdict=None)
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(
        record=record, jd_markdown="# JD", screening_markdown=None,
    )

    response = host.handle_get_detail(
        {"action": "get_detail", "url": "https://www.wanted.co.kr/wd/123456"}, repository
    )

    assert response["data"]["is_fallback"] is False


# --- handle_get_company_info ---


class _FakeCompanyInfoService:
    def __init__(self, lookup_by_company: dict[str, object] | None = None) -> None:
        self._lookup_by_company = lookup_by_company or {}

    def inspect(self, company: str) -> object:
        return self._lookup_by_company.get(company, _company_lookup(status="missing"))


def _company_lookup(
    *,
    status: str,
    markdown_path: Path | None = None,
    completeness: float | None = None,
) -> object:
    validation = None
    if markdown_path is not None:
        validation = SimpleNamespace(file_path=markdown_path, completeness_score=completeness)
    return SimpleNamespace(status=status, file_path=markdown_path, validation=validation)


def test_handle_get_company_info_returns_markdown(tmp_path: Path):
    info_file = tmp_path / "acme.md"
    info_file.write_text("# Acme Corp\n\n## 기업 정보\n\n| 항목 | 내용 |\n|--|--|\n| 설립 | 2020 |", encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService(
        {"Acme": _company_lookup(status="ready", markdown_path=info_file, completeness=100.0)}
    )

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response["status"] == "ok"
    assert response["data"] == {
        "status": "ready",
        "warning_code": None,
        "completeness": 100.0,
        "company_info_markdown": info_file.read_text(encoding="utf-8"),
    }


def test_handle_get_company_info_no_file():
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response == {
        "status": "ok",
        "data": {
            "status": "warning",
            "warning_code": "missing",
            "completeness": None,
            "company_info_markdown": None,
        },
    }


def test_handle_get_company_info_incomplete_file_returns_warning_with_markdown(tmp_path: Path):
    info_file = tmp_path / "acme.md"
    info_file.write_text("# Acme Corp\n\n## 기업 정보\n\n| 항목 | 내용 |\n|--|--|\n| 설립 | 2020 |", encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService(
        {"Acme": _company_lookup(status="incomplete", markdown_path=info_file, completeness=50.0)}
    )

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response["status"] == "ok"
    assert response["data"] == {
        "status": "warning",
        "warning_code": "below_threshold",
        "completeness": 50.0,
        "company_info_markdown": info_file.read_text(encoding="utf-8"),
    }


def test_handle_get_company_info_invalid_file_returns_safe_error(tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
        _FakeCompanyInfoService({"Acme": _company_lookup(status="invalid", markdown_path=missing)}),
    )

    assert response == {"status": "error", "message": "company info unavailable"}


def test_handle_get_company_info_unsafe_file_returns_safe_error(tmp_path: Path):
    unsafe_path = tmp_path / "unsafe.md"
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
        _FakeCompanyInfoService({"Acme": _company_lookup(status="unsafe", markdown_path=unsafe_path)}),
    )

    assert response == {"status": "error", "message": "company info unavailable"}


def test_handle_get_company_info_unresolvable_key():
    repository = MagicMock()
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://example.com/not-a-job"},
        repository, service,
    )

    assert response == {"status": "error", "message": "company info unavailable"}
    repository.get.assert_not_called()


def test_handle_get_company_info_record_not_found():
    repository = MagicMock()
    repository.get.side_effect = JobRecordNotFound("wanted", "999999")
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/999999"},
        repository, service,
    )

    assert response == {"status": "error", "message": "company info unavailable"}


def test_handle_get_company_info_empty_company():
    """When record.company is falsy the handler returns null without scanning."""
    record = _make_record()
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    repository = MagicMock()
    repository.get.return_value = stored
    service = _FakeCompanyInfoService()
    # Patch company to empty after construction (model rejects blank in __post_init__)
    object.__setattr__(stored.record, "company", "")

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response == {"status": "error", "message": "company info unavailable"}


def test_handle_get_company_info_oversized_response(tmp_path: Path):
    info_file = tmp_path / "big.md"
    info_file.write_text("x" * 1_000_000, encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService(
        {"Acme": _company_lookup(status="ready", markdown_path=info_file, completeness=100.0)}
    )

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response == {"status": "error", "message": "company info unavailable"}


def test_dispatch_routes_get_company_info(tmp_path: Path):
    info_file = tmp_path / "acme.md"
    info_file.write_text("# Acme", encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService(
        {"Acme": _company_lookup(status="ready", markdown_path=info_file, completeness=100.0)}
    )

    response, work_item = host.dispatch(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
        company_info_service=service,
    )

    assert response["status"] == "ok"
    assert response["data"]["company_info_markdown"] == "# Acme"
    assert work_item is None


def test_dispatch_get_company_info_without_service():
    repository = MagicMock()

    response, work_item = host.dispatch(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
    )

    assert response == {"status": "error", "message": "company info unavailable"}
    assert work_item is None
