from __future__ import annotations

from pathlib import Path
import re


def test_only_storage_adapter_constructs_canonical_record_paths() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "careerkit" / "jobs"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"file_records.py", "storage.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"/\s*[\"']records[\"']", text):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
