from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from tests.resume.render_baseline import (
    _page_count,
    _pdftotext_normalized,
    _rasterize_pdf,
)
from tests.contract._u8_final_proof import (
    REPO_ROOT,
    baseline_now,
    example_output_thresholds,
    generate_example_outputs,
    load_example_manifest,
    normalized_output,
)
from tests.resume.pdf_visual_equivalence import compare_ppm_pages


def verify_render_baseline(work_root: Path) -> None:
    manifest = load_example_manifest()
    build_dir = generate_example_outputs(work_root, now=baseline_now(manifest))
    raster_root = work_root / "rendered-pages"
    thresholds = example_output_thresholds()

    text_outputs = cast(dict[str, dict[str, str]], manifest["text_outputs"])
    for relative, payload in text_outputs.items():
        assert normalized_output(build_dir / relative) == payload["normalized_content"], relative

    pdf_outputs = cast(dict[str, dict[str, Any]], manifest["pdf_outputs"])
    for relative, payload in pdf_outputs.items():
        pdf_path = build_dir / relative
        assert _pdftotext_normalized(pdf_path) == payload["normalized_pdftotext"], relative
        assert _page_count(pdf_path) == payload["page_count"], relative
        candidate_pages = _rasterize_pdf(
            pdf_path,
            raster_root / Path(relative).stem,
            dpi=cast(int, manifest["raster_dpi"]),
            output_root=raster_root,
        )
        assert len(candidate_pages) == len(payload["pages"]), relative
        for expected_page, candidate_page in zip(payload["pages"], candidate_pages, strict=True):
            expected_file = cast(str, expected_page["file"])
            candidate_file = cast(str, candidate_page["file"])
            comparison = compare_ppm_pages(
                REPO_ROOT / "tests" / "fixtures" / "resume" / "example-render-baseline" / expected_file,
                raster_root / candidate_file,
                thresholds,
                diagnostics_dir=work_root / "ppm-diagnostics",
            )
            assert comparison.matches, f"{relative} {expected_file}: {comparison.reason}"
