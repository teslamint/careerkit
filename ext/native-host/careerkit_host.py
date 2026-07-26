"""Chrome Native Messaging host bridging the careerkit browser extension to
the local careerkit Python backend.

Protocol: each message is a 4-byte little-endian length prefix followed by a
UTF-8 JSON body, on both stdin (requests) and stdout (responses). See
https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging
"""

from __future__ import annotations

import json
import queue
import re
import struct
import sys
import threading
import uuid
from typing import Any, BinaryIO

from urllib.parse import urlparse

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository, JobRecordNotFound
from careerkit.jobs.application.automation import JobsExtractionStage, JobsScreeningStage, load_candidate_context
from careerkit.jobs.application.company_info import CompanyInfoService
from careerkit.jobs.application.screening import is_fallback_document
from careerkit.jobs.application.storage_migration import extract_job_id, get_platform_from_url
from careerkit.jobs.domain.model import JobKey, ScreeningVerdict
from careerkit.workspace import resolve_workspace

_MAX_MESSAGE_BYTES = 1_048_576  # 1 MB defensive cap on incoming messages
_MAX_RESPONSE_BYTES = 900_000

_VERDICT_LABELS = {
    ScreeningVerdict.RECOMMENDED: "지원 추천",
    ScreeningVerdict.HOLD: "지원 보류",
    ScreeningVerdict.NOT_RECOMMENDED: "지원 비추천",
}


def _safe_error_message(exc: Exception) -> str:
    """Strip filesystem paths from an exception message before it reaches the extension."""
    msg = re.sub(r"/(?:Users|home)/\S+", "<path>", str(exc))
    return msg[:200]


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one length-prefixed JSON message from `stream`.

    Returns None on a clean EOF (stdin closed), signalling the main loop to
    exit.
    """
    raw_length = stream.read(4)
    if not raw_length:
        return None
    if len(raw_length) < 4:
        raise ValueError("truncated length prefix")
    (length,) = struct.unpack("<I", raw_length)
    if length > _MAX_MESSAGE_BYTES:
        _drain(stream, length)
        raise ValueError(f"message too large: {length} bytes (limit {_MAX_MESSAGE_BYTES})")
    body = stream.read(length)
    if len(body) < length:
        raise ValueError("truncated message body")
    return json.loads(body.decode("utf-8"))


def _drain(stream: BinaryIO, length: int) -> None:
    """Consume and discard `length` bytes in bounded chunks to keep the stream aligned."""
    remaining = length
    while remaining > 0:
        chunk = stream.read(min(remaining, 65_536))
        if not chunk:
            break
        remaining -= len(chunk)


def write_message(stream: BinaryIO, obj: dict[str, Any]) -> None:
    """Write `obj` as a length-prefixed JSON message to `stream`."""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


_ALLOWED_HOSTS: dict[str, str] = {
    "www.wanted.co.kr": "wanted",
    "wanted.co.kr": "wanted",
    "www.rememberapp.co.kr": "remember",
    "groupby.kr": "groupby",
    "www.groupby.kr": "groupby",
    "www.saramin.co.kr": "saramin",
}


def _resolve_key(msg: dict[str, Any]) -> JobKey | None:
    url = msg.get("url")
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    host = parsed.hostname or ""
    if host.endswith(".rememberapp.co.kr"):
        host = "www.rememberapp.co.kr"
    if host not in _ALLOWED_HOSTS:
        return None
    platform = get_platform_from_url(url)
    job_id = extract_job_id(url)
    if platform is None or job_id is None:
        return None
    return JobKey(platform, job_id)


def handle_ping(msg: dict[str, Any], repository: JDRecordRepository) -> dict[str, Any]:
    return {"status": "ok"}


def handle_lookup(msg: dict[str, Any], repository: JDRecordRepository) -> dict[str, Any]:
    key = _resolve_key(msg)
    if key is None:
        return {"status": "ok", "data": None}
    stored = repository.find(key)
    if stored is None:
        return {"status": "ok", "data": None}
    return {"status": "ok", "data": stored.record.to_dict()}


def handle_get_detail(msg: dict[str, Any], repository: JDRecordRepository) -> dict[str, Any]:
    key = _resolve_key(msg)
    if key is None:
        return {"status": "ok", "data": None}
    try:
        stored = repository.get(key)
    except JobRecordNotFound:
        return {"status": "ok", "data": None}

    is_fallback = (
        stored.screening_markdown is not None
        and is_fallback_document(stored.screening_markdown)
    )
    data: dict[str, Any] = {
        "record": stored.record.to_dict(),
        "jd_markdown": stored.jd_markdown,
        "screening_markdown": stored.screening_markdown,
        "is_fallback": is_fallback,
    }
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        data["jd_markdown"] = None
        data["truncated"] = True
    return {"status": "ok", "data": data}


def handle_get_company_info(
    msg: dict[str, Any],
    repository: JDRecordRepository,
    company_info_service: CompanyInfoService,
) -> dict[str, Any]:
    key = _resolve_key(msg)
    if key is None:
        return {"status": "ok", "data": None}
    try:
        stored = repository.get(key)
    except JobRecordNotFound:
        return {"status": "ok", "data": None}
    company = stored.record.company
    if not company:
        return {"status": "ok", "data": None}
    path = company_info_service.find_matching_file(company)
    if path is None:
        return {"status": "ok", "data": None}
    try:
        markdown = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"status": "ok", "data": None}
    if len(json.dumps({"company_info_markdown": markdown}, ensure_ascii=False).encode("utf-8")) > _MAX_RESPONSE_BYTES:
        return {"status": "ok", "data": None}
    return {"status": "ok", "data": {"company_info_markdown": markdown}}


_MAX_IN_FLIGHT = 10


def handle_rescreen(
    msg: dict[str, Any],
    repository: JDRecordRepository,
    work_queue: "queue.Queue[tuple[str, str, bool]]",
    in_flight: set[str] | None = None,
    in_flight_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    key = _resolve_key(msg)
    if key is None:
        return {"status": "error", "message": "unrecognized or unsupported job URL"}

    url = msg.get("url")

    stored = repository.find(key)
    if stored is None:
        return {"status": "error", "message": "record not found — collect first"}

    if in_flight is not None and in_flight_lock is not None:
        with in_flight_lock:
            if url in in_flight:
                return {"status": "error", "message": "already queued"}
            if len(in_flight) >= _MAX_IN_FLIGHT:
                return {"status": "error", "message": "queue full"}

    tracking_id = str(uuid.uuid4())
    if in_flight is not None and in_flight_lock is not None:
        with in_flight_lock:
            in_flight.add(url)
    work_queue.put((url, tracking_id, True))
    return {"status": "accepted", "action": "rescreen", "tracking_id": tracking_id}


def handle_collect(
    msg: dict[str, Any],
    repository: JDRecordRepository,
    work_queue: "queue.Queue[tuple[str, str]]",
    in_flight: set[str] | None = None,
    in_flight_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """`{status: "duplicate", data: <record dict>}` / `{status: "accepted",
    tracking_id}` — shapes fixed by the already-shipped background/content
    scripts (`service-worker.js` reads `response.tracking_id` top-level,
    `panel.js` checks `response.status === "duplicate"`, `badge.js` renders
    `response.data` as a record dict via the same path as `lookup`)."""
    key = _resolve_key(msg)
    if key is None:
        return {"status": "error", "message": "unrecognized or unsupported job URL"}

    url = msg.get("url")

    if in_flight is not None and in_flight_lock is not None:
        with in_flight_lock:
            if url in in_flight:
                return {"status": "error", "message": "already queued"}
            if len(in_flight) >= _MAX_IN_FLIGHT:
                return {"status": "error", "message": "queue full"}

    stored = repository.find(key)
    if stored is not None:
        if stored.record.screening_verdict is not None:
            return {"status": "duplicate", "action": "collect", "data": stored.record.to_dict()}
        tracking_id = str(uuid.uuid4())
        if in_flight is not None and in_flight_lock is not None:
            with in_flight_lock:
                in_flight.add(url)
        work_queue.put((url, tracking_id, True))
        return {"status": "accepted", "action": "collect", "tracking_id": tracking_id}

    tracking_id = str(uuid.uuid4())
    if in_flight is not None and in_flight_lock is not None:
        with in_flight_lock:
            in_flight.add(url)
    work_queue.put((url, tracking_id, False))
    return {"status": "accepted", "action": "collect", "tracking_id": tracking_id}


_HANDLERS = {
    "ping": handle_ping,
    "lookup": handle_lookup,
    "get_detail": handle_get_detail,
}


_worker_context: dict[str, Any] | None = None


def _ensure_worker_alive() -> None:
    """Restart worker thread if it died unexpectedly (P2#17)."""
    ctx = _worker_context
    if ctx is None:
        return
    worker: threading.Thread = ctx["worker"]
    if worker.is_alive():
        return

    work_queue: queue.Queue = ctx["worker_args"][0]
    stdout: BinaryIO = ctx["worker_args"][4]
    stdout_lock: threading.Lock = ctx["worker_args"][5]

    orphaned: list[tuple] = []
    while not work_queue.empty():
        try:
            orphaned.append(work_queue.get_nowait())
        except queue.Empty:
            break

    for item in orphaned:
        url, tracking_id = item[0], item[1]
        with stdout_lock:
            write_message(
                stdout,
                {"type": "screening_failed", "tracking_id": tracking_id, "message": "worker restarted"},
            )
        work_queue.task_done()

    in_flight: set[str] | None = ctx.get("in_flight")
    if in_flight is not None:
        in_flight_lock: threading.Lock = ctx.get("in_flight_lock")
        with in_flight_lock:
            in_flight.clear()

    new_worker = threading.Thread(
        target=run_screening_worker,
        args=ctx["worker_args"],
        daemon=True,
    )
    new_worker.start()
    ctx["worker"] = new_worker


def dispatch(
    msg: dict[str, Any],
    repository: JDRecordRepository,
    work_queue: "queue.Queue[tuple[str, str]] | None" = None,
    in_flight: set[str] | None = None,
    in_flight_lock: threading.Lock | None = None,
    company_info_service: CompanyInfoService | None = None,
) -> dict[str, Any]:
    action = msg.get("action")
    if action == "get_company_info":
        if company_info_service is None:
            return {"status": "ok", "data": None}
        return handle_get_company_info(msg, repository, company_info_service)
    if action in ("collect", "rescreen"):
        if work_queue is None:
            return {"status": "error", "message": f"{action} action unavailable: queue not configured"}
        _ensure_worker_alive()
        if action == "rescreen":
            return handle_rescreen(msg, repository, work_queue, in_flight, in_flight_lock)
        return handle_collect(msg, repository, work_queue, in_flight, in_flight_lock)
    handler = _HANDLERS.get(action)
    if handler is None:
        return {"status": "error", "message": f"unknown action: {action!r}"}
    return handler(msg, repository)


def run_screening_worker(
    work_queue: "queue.Queue[tuple[str, str, bool]]",
    repository: JDRecordRepository,
    extraction_stage: JobsExtractionStage,
    screening_stage: JobsScreeningStage,
    stdout: BinaryIO,
    stdout_lock: threading.Lock,
    in_flight: set[str] | None = None,
    in_flight_lock: threading.Lock | None = None,
) -> None:
    """Serially drain `work_queue`, extracting and screening one URL at a time.

    Runs on a daemon thread started by `main()`. Writes `screening_complete` /
    `screening_failed` push messages to `stdout`, guarded by `stdout_lock`
    since the main thread also writes responses there.

    Push shapes are fixed by the already-shipped `service-worker.js` /
    `badge.js`: `type` and `tracking_id` are top-level; a failure carries a
    top-level `message` string (`onScreeningFailed` reads `message.message`);
    a success carries `data` shaped as a full record dict — the same shape
    `handle_lookup`/`handle_get_detail` return — since `badge.js` renders it
    via the identical `renderFromRecord(data)` path, plus a `verdict_label`
    for the desktop notification text.
    """
    while True:
        url, tracking_id, screening_only = work_queue.get()
        try:
            batch = extraction_stage.extract([url], dry_run=False, screening_only=screening_only)
            if not batch.records:
                failures = batch.metadata.get("failures") or []
                error = failures[0]["error"] if failures else "extraction failed"
                with stdout_lock:
                    write_message(
                        stdout,
                        {"type": "screening_failed", "tracking_id": tracking_id, "message": error},
                    )
                continue

            screening_result = screening_stage.screen(batch, dry_run=False, llm_timeout=180)

            key = batch.records[0].record.key
            item_id = f"{key.platform}:{key.job_id}"

            screening_failures = screening_result.metadata.get("failures") or []
            record_failure = next(
                (f for f in screening_failures if f.get("job_key") == item_id),
                None,
            )
            if record_failure is not None:
                with stdout_lock:
                    write_message(
                        stdout,
                        {"type": "screening_failed", "tracking_id": tracking_id, "message": record_failure["error"]},
                    )
                continue

            stored = repository.find(key)
            verdict = stored.record.screening_verdict if stored is not None else None
            if verdict is None:
                with stdout_lock:
                    write_message(
                        stdout,
                        {"type": "screening_failed", "tracking_id": tracking_id, "message": "screening produced no verdict"},
                    )
                continue

            data = stored.record.to_dict()
            data["verdict_label"] = _VERDICT_LABELS.get(verdict)
            with stdout_lock:
                write_message(
                    stdout,
                    {"type": "screening_complete", "tracking_id": tracking_id, "data": data},
                )
        except Exception as exc:  # noqa: BLE001 - report to extension, keep worker alive
            with stdout_lock:
                write_message(
                    stdout,
                    {"type": "screening_failed", "tracking_id": tracking_id, "message": _safe_error_message(exc)},
                )
        finally:
            if in_flight is not None and in_flight_lock is not None:
                with in_flight_lock:
                    in_flight.discard(url)
            work_queue.task_done()


def serve(
    stdin: BinaryIO,
    stdout: BinaryIO,
    repository: JDRecordRepository,
    *,
    work_queue: "queue.Queue[tuple[str, str]] | None" = None,
    stdout_lock: threading.Lock | None = None,
    in_flight: set[str] | None = None,
    in_flight_lock: threading.Lock | None = None,
    company_info_service: CompanyInfoService | None = None,
) -> None:
    """Read/dispatch/write messages until stdin closes."""
    lock = stdout_lock or threading.Lock()
    while True:
        try:
            msg = read_message(stdin)
        except (ValueError, json.JSONDecodeError) as exc:
            with lock:
                write_message(stdout, {"status": "error", "message": str(exc)})
            continue

        if msg is None:
            break

        request_id = msg.get("request_id") if isinstance(msg, dict) else None

        try:
            response = dispatch(msg, repository, work_queue, in_flight, in_flight_lock, company_info_service=company_info_service)
        except Exception as exc:  # noqa: BLE001 - report to extension, keep host alive
            response = {"status": "error", "message": _safe_error_message(exc)}

        if request_id is not None:
            response["request_id"] = request_id
        with lock:
            write_message(stdout, response)


def main() -> None:
    global _worker_context

    workspace = resolve_workspace()
    repository = JDRecordRepository(workspace.jobs_records_dir)
    work_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
    stdout_lock = threading.Lock()
    in_flight: set[str] = set()
    in_flight_lock = threading.Lock()

    extraction_stage = JobsExtractionStage(repository=repository)
    screening_stage = JobsScreeningStage(
        workspace=workspace,
        repository=repository,
        candidate_context=load_candidate_context(workspace),
    )
    worker_args = (work_queue, repository, extraction_stage, screening_stage, sys.stdout.buffer, stdout_lock, in_flight, in_flight_lock)
    worker = threading.Thread(
        target=run_screening_worker,
        args=worker_args,
        daemon=True,
    )
    worker.start()

    _worker_context = {
        "worker": worker,
        "worker_args": worker_args,
        "in_flight": in_flight,
        "in_flight_lock": in_flight_lock,
    }

    company_info_service = CompanyInfoService(workspace=workspace)

    serve(
        sys.stdin.buffer, sys.stdout.buffer, repository,
        work_queue=work_queue, stdout_lock=stdout_lock,
        in_flight=in_flight, in_flight_lock=in_flight_lock,
        company_info_service=company_info_service,
    )


if __name__ == "__main__":
    main()
