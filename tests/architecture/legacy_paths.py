from __future__ import annotations

from pathlib import Path


REMOVED_ROOTS = ("scripts", "templates")
REMOVED_FILES = (
    "build.sh",
    "example/interview/build-sheet.py",
    "main.py",
)
REMOVED_PATHS = tuple(Path(path) for path in REMOVED_ROOTS + REMOVED_FILES)
