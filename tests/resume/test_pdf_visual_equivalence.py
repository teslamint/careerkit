from __future__ import annotations

import json
from pathlib import Path

from tests.resume.pdf_visual_equivalence import (
    PdfVisualThresholds,
    build_uniform_rgb,
    compare_ppm_pages,
    mutate_pixels,
    sha256_file,
    write_ppm,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / 'tests' / 'fixtures' / 'resume' / 'pdf-baseline'


def _load_thresholds() -> PdfVisualThresholds:
    values: dict[str, str] = {}
    for line in (ROOT / 'docker' / 'render-versions.env').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        values[key] = value
    return PdfVisualThresholds(
        raster_dpi=int(values['PDF_RASTER_DPI']),
        channel_threshold=int(values['PDF_PIXEL_CHANNEL_THRESHOLD']),
        max_differing_pixel_ratio=float(values['PDF_MAX_DIFFERING_PIXEL_RATIO']),
    )


def test_fixture_manifest_is_synthetic_and_hashes_match() -> None:
    manifest = json.loads((FIXTURE_DIR / 'manifest.json').read_text(encoding='utf-8'))

    assert manifest['schema_version'] == 1
    assert manifest['synthetic'] is True
    assert manifest['raster_dpi'] == 144
    assert manifest['page_count'] == 1
    assert manifest['normalized_pdftotext'].startswith('Synthetic PDF baseline')
    assert manifest['page_sha256']['page-1.ppm'] == sha256_file(FIXTURE_DIR / 'page-1.ppm')


def test_exact_hash_match_short_circuits_pixel_fallback(tmp_path: Path) -> None:
    thresholds = _load_thresholds()
    baseline = FIXTURE_DIR / 'page-1.ppm'
    candidate = tmp_path / 'candidate.ppm'
    candidate.write_bytes(baseline.read_bytes())

    result = compare_ppm_pages(baseline, candidate, thresholds, diagnostics_dir=tmp_path / 'diagnostics')

    assert result.matches is True
    assert result.exact_hash_match is True
    assert result.differing_pixels == 0
    assert result.diagnostic_paths == ()


def test_below_threshold_pixel_delta_is_accepted(tmp_path: Path) -> None:
    thresholds = _load_thresholds()
    baseline = tmp_path / 'baseline.ppm'
    candidate = tmp_path / 'candidate.ppm'
    width = height = 100
    baseline_rgb = build_uniform_rgb(width, height, (30, 30, 30))
    candidate_rgb = mutate_pixels(
        baseline_rgb,
        width,
        [
            (0, 0, (50, 30, 30)),
            (10, 10, (50, 30, 30)),
            (20, 20, (50, 30, 30)),
            (30, 30, (50, 30, 30)),
        ],
    )
    write_ppm(baseline, width, height, baseline_rgb, comment='baseline')
    write_ppm(candidate, width, height, candidate_rgb, comment='below-threshold')

    result = compare_ppm_pages(baseline, candidate, thresholds, diagnostics_dir=tmp_path / 'diagnostics')

    assert result.matches is True
    assert result.exact_hash_match is False
    assert result.differing_pixels == 4
    assert result.differing_ratio == 0.0004
    assert result.diagnostic_paths == ()


def test_above_threshold_pixel_delta_emits_diagnostic_artifacts(tmp_path: Path) -> None:
    thresholds = _load_thresholds()
    baseline = FIXTURE_DIR / 'known-failure-baseline.ppm'
    candidate = FIXTURE_DIR / 'known-failure-candidate.ppm'

    result = compare_ppm_pages(
        baseline,
        candidate,
        thresholds,
        diagnostics_dir=tmp_path / 'diagnostics',
        page_label='page-1',
    )

    assert result.matches is False
    assert result.reason == 'pixel-threshold-exceeded'
    assert result.exact_hash_match is False
    assert result.differing_pixels == 2
    assert len(result.diagnostic_paths) == 3
    for artifact in result.diagnostic_paths:
        assert artifact.exists()
    diff_payload = result.diagnostic_paths[2].read_bytes()
    assert b'amplified-diff' in diff_payload
    assert bytes((255, 0, 0)) in diff_payload
