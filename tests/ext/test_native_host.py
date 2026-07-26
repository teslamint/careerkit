"""Unit tests for the careerkit Native Messaging host."""

from __future__ import annotations

import importlib.util
import io
import json
import queue
import struct
import threading
from pathlib import Path
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

    response = host.dispatch({"action": "not_a_real_action"}, repository)

    assert response["status"] == "error"
    assert "not_a_real_action" in response["message"]


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

    response = host.dispatch({"action": "collect", "url": "https://www.wanted.co.kr/wd/123456"}, repository)

    assert response["status"] == "error"
    repository.find.assert_not_called()


def test_run_screening_worker_writes_screening_complete_on_success():
    record = _make_record(screening_verdict=ScreeningVerdict.RECOMMENDED, verdict_capped=False)
    stored = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown="# S")

    extraction_batch = MagicMock()
    extraction_batch.records = [stored]
    extraction_stage = MagicMock()
    extraction_stage.extract.return_value = extraction_batch

    screening_stage = MagicMock()

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
    push = host.read_message(stdout)
    # data is a full record dict (same shape as lookup/get_detail) plus
    # verdict_label, since badge.js renders it via renderFromRecord(data).
    expected_data = record.to_dict()
    expected_data["verdict_label"] = "지원 추천"
    assert push == {
        "type": "screening_complete",
        "tracking_id": "track-1",
        "data": expected_data,
    }


def test_run_screening_worker_writes_screening_failed_on_extraction_failure():
    extraction_batch = MagicMock()
    extraction_batch.records = []
    extraction_batch.metadata = {"failures": [{"item_id": "wanted:123456", "error": "boom"}]}
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
    assert push == {"type": "screening_failed", "tracking_id": "track-2", "message": "boom"}


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
        "failures": [{"job_key": "wanted:123456", "error": "company info file missing"}],
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
    push = host.read_message(stdout)
    assert push == {
        "type": "screening_failed",
        "tracking_id": "track-3",
        "message": "company info file missing",
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
    push = host.read_message(stdout)
    assert push == {
        "type": "screening_failed",
        "tracking_id": "track-4",
        "message": "screening produced no verdict",
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

    response = host.dispatch({"action": "rescreen", "url": "https://www.wanted.co.kr/wd/123456"}, repository)

    assert response["status"] == "error"
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
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}

    def find_matching_file(self, company: str) -> "Path | None":
        val = self._mapping.get(company)
        if val is None:
            return None
        return Path(val)


def test_handle_get_company_info_returns_markdown(tmp_path: Path):
    info_file = tmp_path / "acme.md"
    info_file.write_text("# Acme Corp\n\n## 기업 정보\n\n| 항목 | 내용 |\n|--|--|\n| 설립 | 2020 |", encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService({"Acme": str(info_file)})

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response["status"] == "ok"
    assert response["data"]["company_info_markdown"] == info_file.read_text(encoding="utf-8")


def test_handle_get_company_info_no_file():
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response == {"status": "ok", "data": None}


def test_handle_get_company_info_read_error(tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"

    class _ServiceWithMissingPath:
        def find_matching_file(self, company: str) -> Path | None:
            return missing

    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, _ServiceWithMissingPath(),
    )

    assert response == {"status": "ok", "data": None}


def test_handle_get_company_info_unresolvable_key():
    repository = MagicMock()
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://example.com/not-a-job"},
        repository, service,
    )

    assert response == {"status": "ok", "data": None}
    repository.get.assert_not_called()


def test_handle_get_company_info_record_not_found():
    repository = MagicMock()
    repository.get.side_effect = JobRecordNotFound("wanted", "999999")
    service = _FakeCompanyInfoService()

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/999999"},
        repository, service,
    )

    assert response == {"status": "ok", "data": None}


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

    assert response == {"status": "ok", "data": None}


def test_handle_get_company_info_oversized_response(tmp_path: Path):
    info_file = tmp_path / "big.md"
    info_file.write_text("x" * 1_000_000, encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService({"Acme": str(info_file)})

    response = host.handle_get_company_info(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository, service,
    )

    assert response == {"status": "ok", "data": None}


def test_dispatch_routes_get_company_info(tmp_path: Path):
    info_file = tmp_path / "acme.md"
    info_file.write_text("# Acme", encoding="utf-8")
    record = _make_record()
    repository = MagicMock()
    repository.get.return_value = StoredJobRecord(record=record, jd_markdown="# JD", screening_markdown=None)
    service = _FakeCompanyInfoService({"Acme": str(info_file)})

    response = host.dispatch(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
        company_info_service=service,
    )

    assert response["status"] == "ok"
    assert response["data"]["company_info_markdown"] == "# Acme"


def test_dispatch_get_company_info_without_service():
    repository = MagicMock()

    response = host.dispatch(
        {"action": "get_company_info", "url": "https://www.wanted.co.kr/wd/123456"},
        repository,
    )

    assert response == {"status": "ok", "data": None}
