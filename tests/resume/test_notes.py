from __future__ import annotations

from careerkit.resume.application.notes import write_notes


def test_write_notes_uses_default_output_parent(tmp_path) -> None:
    base_path = tmp_path / "resume-job-base.md"
    current_path = tmp_path / "resume-job.md"
    output_path = tmp_path / "private" / "build" / "resume-job-notes.md"
    base_path.write_text("# Base\n", encoding="utf-8")
    current_path.write_text("# Current\n", encoding="utf-8")
    result = write_notes(base_path, current_path, output_path, target="CompanyA")
    assert result is not None
    assert output_path.exists()
    assert "Target: CompanyA" in output_path.read_text(encoding="utf-8")


def test_write_notes_creates_parent_dirs_for_custom_output(tmp_path) -> None:
    base_path = tmp_path / "resume-job-base.md"
    current_path = tmp_path / "resume-job.md"
    output_path = tmp_path / "nested" / "output" / "resume-job-notes.md"
    base_path.write_text("# Base\n", encoding="utf-8")
    current_path.write_text("# Current\n", encoding="utf-8")
    write_notes(base_path, current_path, output_path)
    assert output_path.exists()
