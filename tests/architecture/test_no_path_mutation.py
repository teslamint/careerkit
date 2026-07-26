from __future__ import annotations

from pathlib import Path


def test_no_sys_path_mutation_in_installable_or_top_level_tests() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for base in (root / "src" / "careerkit", root / "tests"):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name == "test_no_path_mutation.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "sys.path.insert" in text or "sys.path.append" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
