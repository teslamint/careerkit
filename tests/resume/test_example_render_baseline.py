from __future__ import annotations

import hashlib
import json
from pathlib import Path

from careerkit.resume.adapters import document_renderer
from careerkit.resume.adapters.filesystem import ResumeWorkspaceAdapter
from careerkit.resume.application.career_description import build_career
from tests.contract._u8_final_proof import baseline_now
from tests.resume.pdf_visual_equivalence import sha256_file
from tests.resume import render_baseline

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "resume" / "example-render-baseline"

EXPECTED_TEXT_OUTPUTS = {
    "resume-example.md",
    "resume-example.html",
    "resume-example-remember.txt",
    "resume-example-short.md",
    "resume-example-short.html",
    "resume-example-wanted.txt",
    "career-description-example.md",
    "career-description-example.html",
}

EXPECTED_PDF_OUTPUTS = {
    "resume-example.pdf",
    "resume-example-short.pdf",
    "career-description-example.pdf",
}


def test_example_career_baseline_uses_separator_free_pdf_content(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "example"
    (tmp_path / ".career-workspace").write_text("1\n", encoding="utf-8")
    (base / "variant_config.json").parent.mkdir(parents=True, exist_ok=True)
    (base / "variant_config.json").write_text(
        json.dumps(
            {
                "public": {
                    "companies": ["Acme"],
                    "company_detail": {"Acme": "full"},
                },
                "job": {"companies": []},
            }
        ),
        encoding="utf-8",
    )
    profile = base / "companies/Acme/profile.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "# Acme\n\n## Overview\n- Period: 2020.01 - 2022.01\n- Role: Engineer\n",
        encoding="utf-8",
    )
    captured: dict[str, str | None] = {}

    def fake_bundle(markdown_content: str, **kwargs) -> None:
        if kwargs["markdown_path"].name == "career-description-example.md":
            captured["markdown"] = markdown_content
            captured["pdf"] = kwargs.get("render_markdown_content")

    monkeypatch.setattr(document_renderer, "render_markdown_bundle", fake_bundle)
    monkeypatch.setattr(document_renderer, "render_pdf_markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(document_renderer, "write_text_output", lambda *args, **kwargs: None)

    render_baseline._build_example_outputs(tmp_path)

    adapter = ResumeWorkspaceAdapter(base_dir=base)
    assert captured["markdown"] == build_career(adapter, "public")
    assert captured["pdf"] == build_career(adapter, "public", format_type="pdf")
    assert isinstance(captured["markdown"], str)
    assert isinstance(captured["pdf"], str)
    assert "\n\n---\n\n" in captured["markdown"]
    assert "\n\n---\n\n" not in captured["pdf"]


def test_real_example_render_manifest_exists_and_is_not_synthetic() -> None:
    manifest_path = FIXTURE_DIR / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["synthetic"] is False
    assert manifest["workspace_source"] == "example/"
    assert manifest["image_id"].startswith("sha256:")
    assert manifest["raster_dpi"] == 144
    assert set(manifest["expected_filenames"]) == EXPECTED_TEXT_OUTPUTS | EXPECTED_PDF_OUTPUTS
    assert set(manifest["tool_versions"]) >= {"python", "pandoc", "weasyprint", "pdftotext", "pdftoppm", "pdfinfo"}


def test_current_example_career_markdown_matches_baseline() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    adapter = ResumeWorkspaceAdapter(base_dir=ROOT / "example")
    current = render_baseline._normalize_text(build_career(adapter, "public", now=baseline_now(manifest)))

    assert current == manifest["text_outputs"]["career-description-example.md"]["normalized_content"]


def test_text_outputs_are_complete_and_hashed() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    outputs = manifest["text_outputs"]

    assert set(outputs) == EXPECTED_TEXT_OUTPUTS
    for relative, payload in outputs.items():
        normalized = payload["normalized_content"]
        assert normalized.endswith("\n")
        assert "Synthetic PDF baseline" not in normalized
        assert payload["normalized_sha256"] == hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def test_pdf_outputs_have_real_raster_artifacts_and_hashes_match() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    outputs = manifest["pdf_outputs"]

    assert set(outputs) == EXPECTED_PDF_OUTPUTS
    for relative, payload in outputs.items():
        assert payload["page_count"] >= 1
        assert payload["normalized_pdftotext"].endswith("\n")
        assert payload["normalized_pdftotext_sha256"] == hashlib.sha256(
            payload["normalized_pdftotext"].encode("utf-8")
        ).hexdigest()
        pages = payload["pages"]
        assert len(pages) == payload["page_count"]
        for page in pages:
            path = FIXTURE_DIR / page["file"]
            assert path.exists(), f"missing raster artifact for {relative}: {page['file']}"
            assert page["sha256"] == sha256_file(path)
