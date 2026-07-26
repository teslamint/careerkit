from __future__ import annotations

import subprocess
import sys


def test_importing_migrated_modules_does_not_eagerly_load_optional_dependencies() -> None:
    code = """
import importlib
import sys

targets = [
    "careerkit.jobs.domain.model",
    "careerkit.jobs.domain.verdict",
    "careerkit.jobs.application.status",
    "careerkit.jobs.adapters.storage.file_records",
    "careerkit.resume.application.build",
]
for name in targets:
    importlib.import_module(name)

blocked = sorted(
    name for name in sys.modules
    if name.startswith(("sentence_transformers", "playwright", "patchright"))
)
if blocked:
    raise SystemExit(",".join(blocked))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
