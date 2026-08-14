"""Integration tests for the careerkit Native Messaging host: process-level
protocol behavior (subprocess + stdin/stdout) and the install flow."""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.domain.model import JobKey, JobRecord, ScreeningVerdict
from careerkit.workspace import MARKER_FILE_NAME, MARKER_VERSION

_NATIVE_HOST_DIR = Path(__file__).parents[2] / "ext" / "native-host"
_INSTALL_PATH = _NATIVE_HOST_DIR / "install.py"
_HOST_PATH = _NATIVE_HOST_DIR / "careerkit_host.py"


def _load_install_module():
    spec = importlib.util.spec_from_file_location("careerkit_install", _INSTALL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame(msg: dict) -> bytes:
    payload = json.dumps(msg).encode("utf-8")
    return struct.pack("<I", len(payload)) + payload


def _unframe_all(data: bytes) -> list[dict]:
    messages: list[dict] = []
    offset = 0
    while offset < len(data):
        (length,) = struct.unpack("<I", data[offset : offset + 4])
        offset += 4
        messages.append(json.loads(data[offset : offset + length]))
        offset += length
    return messages


def _run_host(workspace_root: Path, messages: list[dict]) -> list[dict]:
    """Run careerkit_host.py as a subprocess, send `messages` over stdin, and
    return every framed response/push read from stdout before the process
    exits (stdin is closed after writing, ending the main loop)."""
    stdin_bytes = b"".join(_frame(msg) for msg in messages)
    env = {"CAREER_WORKSPACE": str(workspace_root)}
    proc = subprocess.run(
        [sys.executable, str(_HOST_PATH)],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return _unframe_all(proc.stdout)


def _run_host_with_fixture_worker(workspace_root: Path, messages: list[dict], tmp_path: Path) -> list[dict]:
    driver = tmp_path / "host_driver.py"
    driver.write_text(
        dedent(
            f"""
            import importlib.util
            import io
            import json
            import struct
            import sys
            from pathlib import Path
            from types import SimpleNamespace

            from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
            from careerkit.jobs.domain.model import JobKey, JobRecord, ScreeningVerdict

            host_path = Path({str(_HOST_PATH)!r})
            spec = importlib.util.spec_from_file_location("careerkit_host_test_driver", host_path)
            assert spec is not None and spec.loader is not None
            host = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host)

            workspace_root = Path(sys.argv[1])
            repository = JDRecordRepository(workspace_root / "private" / "jd" / "records")

            class FixtureExtractionStage:
                def extract(self, urls, *, dry_run, screening_only):
                    url = urls[0]
                    key = JobKey(host.get_platform_from_url(url), host.extract_job_id(url))
                    stored = repository.find(key)
                    if stored is None:
                        record = JobRecord(
                            platform=key.platform,
                            job_id=key.job_id,
                            company="Fixture Corp",
                            position="Backend Engineer",
                            source_url=url,
                        )
                        repository.create(record, jd_markdown="# JD")
                        stored = repository.get(key)
                    return SimpleNamespace(
                        records=[stored],
                        metadata={{"failures": []}},
                        company_contexts={{f"{{key.platform}}:{{key.job_id}}": object()}},
                    )

            class FixtureScreeningStage:
                def screen(self, extraction, *, dry_run, llm_timeout):
                    key = extraction.records[0].record.key
                    repository.update_screening_result(
                        key,
                        screening_markdown="# Screening body",
                        screening_verdict=ScreeningVerdict.RECOMMENDED,
                    )
                    item_id = f"{{key.platform}}:{{key.job_id}}"
                    return SimpleNamespace(
                        metadata={{
                            "failures": [],
                            "company_info_results": {{
                                item_id: {{
                                    "status": "ready",
                                    "attempted": True,
                                    "persisted": True,
                                    "completeness": 100.0,
                                    "warning_code": None,
                                }}
                            }},
                        }}
                    )

            raw = sys.stdin.buffer.read()
            stdin = io.BytesIO(raw)
            stdout = io.BytesIO()
            host.serve_with_worker(
                stdin,
                stdout,
                repository,
                extraction_stage=FixtureExtractionStage(),
                screening_stage=FixtureScreeningStage(),
                company_info_service=None,
            )
            sys.stdout.buffer.write(stdout.getvalue())
            """
        ),
        encoding="utf-8",
    )
    stdin_bytes = b"".join(_frame(msg) for msg in messages)
    proc = subprocess.run(
        [sys.executable, str(driver), str(workspace_root)],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"CAREER_WORKSPACE": str(workspace_root)},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return _unframe_all(proc.stdout)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "private" / "jd" / "records").mkdir(parents=True)
    (root / MARKER_FILE_NAME).write_text(MARKER_VERSION, encoding="utf-8")
    return root


@pytest.fixture
def repository(workspace_root: Path) -> JDRecordRepository:
    return JDRecordRepository(workspace_root / "private" / "jd" / "records")


def test_native_host_ping(workspace_root: Path):
    """Covers the `ping` action at the process level."""
    responses = _run_host(workspace_root, [{"action": "ping"}])

    assert responses == [{"status": "ok"}]


def test_lookup_existing_record(workspace_root: Path, repository: JDRecordRepository):
    """Covers S2: revisiting an already-screened posting."""
    record = JobRecord(
        platform="wanted",
        job_id="123456",
        company="Acme",
        position="Backend Engineer",
        source_url="https://www.wanted.co.kr/wd/123456",
        screening_verdict=ScreeningVerdict.HOLD,
    )
    repository.create(record, jd_markdown="# JD")

    responses = _run_host(workspace_root, [{"action": "lookup", "url": "https://www.wanted.co.kr/wd/123456"}])

    assert responses[0]["status"] == "ok"
    assert responses[0]["data"]["screening_verdict"] == "hold"
    assert responses[0]["data"]["platform"] == "wanted"
    assert responses[0]["data"]["job_id"] == "123456"


def test_collect_duplicate_returns_existing(workspace_root: Path, repository: JDRecordRepository):
    """Covers S5: re-collecting an already-recorded posting returns duplicate + verdict."""
    record = JobRecord(
        platform="wanted",
        job_id="654321",
        company="Beta",
        position="Platform Engineer",
        source_url="https://www.wanted.co.kr/wd/654321",
        screening_verdict=ScreeningVerdict.RECOMMENDED,
    )
    repository.create(record, jd_markdown="# JD")

    responses = _run_host(workspace_root, [{"action": "collect", "url": "https://www.wanted.co.kr/wd/654321"}])

    # panel.js checks `response.status === "duplicate"`; badge.js renders
    # `response.data` as a full record dict, same shape as `lookup`.
    assert responses[0]["status"] == "duplicate"
    assert responses[0]["data"] == record.to_dict()


def test_collect_eof_drains_worker_and_preserves_message_order(workspace_root: Path, tmp_path: Path):
    responses = _run_host_with_fixture_worker(
        workspace_root,
        [{"action": "collect", "url": "https://www.wanted.co.kr/wd/777888"}],
        tmp_path,
    )

    assert responses[0]["status"] == "accepted"
    tracking_id = responses[0]["tracking_id"]
    assert responses[1] == {
        "type": "screening_progress",
        "tracking_id": tracking_id,
        "stage": "company_info",
        "state": "checking",
    }
    assert responses[2] == {
        "type": "screening_progress",
        "tracking_id": tracking_id,
        "stage": "company_info",
        "state": "enriching",
    }
    assert responses[3] == {
        "type": "screening_progress",
        "tracking_id": tracking_id,
        "stage": "screening",
        "state": "running",
    }
    assert responses[4]["type"] == "screening_complete"
    assert responses[4]["tracking_id"] == tracking_id
    assert responses[4]["data"]["company_info"] == {
        "status": "ready",
        "attempted": True,
        "persisted": True,
        "completeness": 100.0,
        "warning_code": None,
    }


def test_get_detail_returns_screening(workspace_root: Path, repository: JDRecordRepository):
    """Covers S4: side panel detail view for a screened posting."""
    key = JobKey("wanted", "111222")
    record = JobRecord(
        platform="wanted",
        job_id="111222",
        company="Gamma",
        position="SRE",
        source_url="https://www.wanted.co.kr/wd/111222",
    )
    repository.create(record, jd_markdown="# JD body")
    repository.update_screening_result(
        key,
        screening_markdown="# Screening body",
        screening_verdict=ScreeningVerdict.HOLD,
    )

    responses = _run_host(workspace_root, [{"action": "get_detail", "url": "https://www.wanted.co.kr/wd/111222"}])

    assert responses[0]["status"] == "ok"
    assert responses[0]["data"]["jd_markdown"] == "# JD body"
    assert responses[0]["data"]["screening_markdown"] == "# Screening body"
    assert responses[0]["data"]["record"]["screening_verdict"] == "hold"


def test_wrapper_from_root_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_root: Path):
    install = _load_install_module()

    fake_generated_dir = tmp_path / "generated"
    fake_chrome_hosts_dir = tmp_path / "chrome-hosts"
    monkeypatch.setattr(install, "GENERATED_DIR", fake_generated_dir)
    monkeypatch.setattr(install, "CHROME_HOSTS_DIR", fake_chrome_hosts_dir)

    install.main()

    wrapper_path = fake_generated_dir / "careerkit_host_wrapper"
    assert wrapper_path.is_file()

    ping_payload = json.dumps({"action": "ping"}).encode("utf-8")
    proc = subprocess.run(
        [str(wrapper_path)],
        input=struct.pack("<I", len(ping_payload)) + ping_payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

    stdout = proc.stdout
    (length,) = struct.unpack("<I", stdout[:4])
    response = json.loads(stdout[4 : 4 + length])

    assert response == {"status": "ok"}


# --- host payload <-> service-worker validator seam ---------------------------
#
# `_completion_company_info` emits the company_info block, and
# `isValidCompanyInfoResult` in ext/background/service-worker.js decides whether
# a completion survives. If either side's key set moves, the service worker
# drops *every* screening_complete silently and the user sees nothing until a
# poll timeout. Testing the two sides separately cannot catch that: each stays
# green while agreeing with nobody. This test runs the real Python payload
# through the real JavaScript validator.

_SERVICE_WORKER_PATH = Path(__file__).parents[2] / "ext" / "background" / "service-worker.js"

_VALIDATOR_DRIVER = dedent(
    """
    "use strict";
    var fs = require("fs");
    var vm = require("vm");

    var workerPath = process.argv[2];
    var code = fs.readFileSync(workerPath, "utf-8");
    var messages = JSON.parse(fs.readFileSync(process.argv[3], "utf-8"));

    function noop() {}
    var listener = { addListener: noop };
    var chromeMock = {
      runtime: {
        id: "test-runtime",
        lastError: null,
        onMessage: listener,
        onInstalled: listener,
        onStartup: listener,
        connectNative: function () { throw new Error("not connected"); },
        getURL: function (value) { return value; },
        sendMessage: noop
      },
      tabs: { sendMessage: noop, query: function (_query, callback) { callback([]); } },
      notifications: { create: noop, clear: noop, onClicked: listener },
      action: {
        getBadgeText: function () { return Promise.resolve(""); },
        setBadgeBackgroundColor: function () { return Promise.resolve(); },
        setBadgeText: function () { return Promise.resolve(); }
      },
      sidePanel: { setPanelBehavior: function () { return Promise.resolve(); } },
      alarms: { create: noop, onAlarm: listener },
      storage: {
        session: {
          set: function () { return Promise.resolve(); },
          get: function () { return Promise.resolve({ pending_screenings: {} }); }
        }
      }
    };

    var sandbox = {
      console: console,
      Promise: Promise,
      Date: Date,
      Map: Map,
      setTimeout: noop,
      clearTimeout: noop,
      setInterval: noop,
      clearInterval: noop,
      module: { exports: {} },
      globalThis: {},
      chrome: chromeMock
    };
    vm.runInNewContext(code, sandbox, { filename: workerPath });
    var api = sandbox.module.exports;

    var out = messages.map(function (message) {
      api.__setPendingScreening(message.tracking_id, { url: "https://example.test/1", tabId: 1 });
      return {
        accepted: api.isValidCompletionMessage(message),
        sanitized: api.sanitizeCompletionData(message.data)
      };
    });
    process.stdout.write(JSON.stringify(out));
    """
).strip()


def _load_host_module():
    spec = importlib.util.spec_from_file_location("careerkit_host_seam", _HOST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completion_message(tracking_id: str, data: dict) -> dict:
    return {"type": "screening_complete", "tracking_id": tracking_id, "data": data}


def _judge_with_service_worker(tmp_path: Path, messages: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found on PATH")
    driver = tmp_path / "validator_driver.cjs"
    driver.write_text(_VALIDATOR_DRIVER, encoding="utf-8")
    payload = tmp_path / "messages.json"
    payload.write_text(json.dumps(messages), encoding="utf-8")
    proc = subprocess.run(
        [node, str(driver), str(_SERVICE_WORKER_PATH), str(payload)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return json.loads(proc.stdout)


def test_completion_company_info_payload_passes_the_service_worker_validator(tmp_path: Path):
    host = _load_host_module()
    results = [
        {"status": "ready", "attempted": True, "persisted": True, "completeness": 100, "warning_code": None},
        {
            "status": "warning",
            "attempted": True,
            "persisted": True,
            "completeness": 62,
            "warning_code": "below_threshold",
        },
        {"status": "warning", "attempted": True, "persisted": False, "completeness": None, "warning_code": "missing"},
    ]
    messages = [
        _completion_message(
            f"track-{index}",
            {
                "company": "Acme",
                "position": "Backend Engineer",
                "screening_verdict": "hold",
                "verdict_capped": False,
                "company_info": host._completion_company_info(result),
            },
        )
        for index, result in enumerate(results)
    ]

    # The set-aside payload this cycle introduced. `data` is `record.to_dict()`
    # plus a label, so the verdict is present and null — not absent — and the
    # reason the content script reads is carried alongside it.
    messages.append(
        _completion_message(
            "track-set-aside",
            {
                "company": "Acme",
                "position": "Backend Engineer",
                "screening_verdict": None,
                "prescreen_reason": "title_exclude",
                "verdict_label": "사전 필터 제외 기록",
                "company_info": host._completion_company_info(results[0]),
            },
        )
    )

    # Sensitivity: the validator compares the key set exactly, so one extra key
    # on the Python side must be rejected. Without this case the assertions
    # below would also hold for a validator that checked nothing.
    drifted = dict(host._completion_company_info(results[0]))
    drifted["extra_key"] = "drift"
    messages.append(
        _completion_message(
            "track-drift",
            {"company": "Acme", "position": "Backend Engineer", "company_info": drifted},
        )
    )

    judged = _judge_with_service_worker(tmp_path, messages)

    assert [entry["accepted"] for entry in judged] == [True, True, True, True, False]
    for entry, message in zip(judged[:4], messages[:4]):
        assert entry["sanitized"] is not None
        assert entry["sanitized"]["company_info"] == message["data"]["company_info"]
    assert judged[3]["sanitized"]["verdict_label"] == "사전 필터 제외 기록"
    # A null verdict survives sanitizing, so "no verdict" alone cannot tell the
    # content script whether the record was decided. The reason must survive too.
    assert judged[3]["sanitized"]["screening_verdict"] is None
    assert judged[3]["sanitized"]["prescreen_reason"] == "title_exclude"
    assert judged[4]["sanitized"] is None
