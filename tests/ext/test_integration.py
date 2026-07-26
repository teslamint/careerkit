"""Integration tests for the careerkit Native Messaging host: process-level
protocol behavior (subprocess + stdin/stdout) and the install flow."""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

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
    proc = subprocess.run(
        [sys.executable, str(_HOST_PATH)],
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
