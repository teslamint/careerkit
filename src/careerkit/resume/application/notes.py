from __future__ import annotations

from datetime import datetime
from pathlib import Path
import difflib


def generate_diff(base_content: str, current_content: str) -> tuple[list[str], int, int]:
    diff = list(
        difflib.unified_diff(
            base_content.splitlines(),
            current_content.splitlines(),
            fromfile="resume-job-base.md",
            tofile="resume-job.md",
            lineterm="\n",
        )
    )
    additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return diff, additions, deletions


def format_notes_entry(target: str, diff_lines: list[str], additions: int, deletions: int, max_lines: int = 200) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    diff_content = f"(diff too large: {len(diff_lines)} lines, showing summary only)" if len(diff_lines) > max_lines else "\n".join(line.rstrip("\n") for line in diff_lines)
    return f"## {date_str} - Target: {target}\n\n- 변경 요약: +{additions}/-{deletions} lines\n\n```diff\n{diff_content}\n```\n\n"


def write_notes(base_path: Path, current_path: Path, output_path: Path, *, target: str = "TBD", clean: bool = False):
    if not base_path.exists() or not current_path.exists():
        raise FileNotFoundError("base or current resume file not found")
    base_content = base_path.read_text(encoding="utf-8")
    current_content = current_path.read_text(encoding="utf-8")
    if base_content == current_content:
        return None
    diff_lines, additions, deletions = generate_diff(base_content, current_content)
    entry = format_notes_entry(target, diff_lines, additions, deletions)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if clean or not output_path.exists():
        output_path.write_text("# Resume Job Notes\n\n" + entry, encoding="utf-8")
    else:
        output_path.write_text(output_path.read_text(encoding="utf-8") + entry, encoding="utf-8")
    return output_path, additions, deletions
