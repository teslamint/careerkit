# Render Environment Baseline

U0 defines a single authoritative render toolchain for PDF equivalence work.
`docker/render.Dockerfile` is the build recipe and `docker/render-versions.env`
is the human-reviewed lock input for renderer packages, raster DPI, and pixel
diff thresholds.

## Locked contract

- Base image: `python:3.12-slim-bookworm`
- APT packages: `pandoc`, `poppler-utils`, `fonts-noto-cjk`
- WeasyPrint runtime libraries: `libpango-1.0-0`, `libpangoft2-1.0-0`
- Python renderer: `weasyprint==67.0`
- Raster DPI: `144`
- Per-channel pixel threshold: `8/255`
- Maximum differing-pixel ratio per page: `0.05%` (`0.0005`)

## Comparison pipeline

1. Normalize `pdftotext` output and require exact text equality.
2. Require exact page-count equality.
3. Rasterize each page at 144 DPI.
4. If page-image SHA-256 hashes match, accept immediately (fast path).
5. Otherwise require identical dimensions and compare pixels.
6. A pixel counts as different when any RGB channel delta is greater than `8`.
7. Accept only when differing pixels are less than or equal to `0.05%` of the page.
8. On failure, emit three page artifacts per page:
   - baseline image copy
   - candidate image copy
   - amplified diff image highlighting differing pixels in red

## Synthetic baseline fixture

`tests/fixtures/resume/pdf-baseline/` is intentionally synthetic. It contains:

- `manifest.json` — baseline metadata, normalized text, page count, raster DPI,
  and image hashes
- `page-1.ppm` — exact-hash baseline sample
- `known-failure-baseline.ppm` / `known-failure-candidate.ppm` — a stable pair
  used to prove diagnostic artifacts are emitted when the tolerance contract is
  exceeded

These fixtures are calibration anchors only; they do not contain any private
resume or job data.

## Real synthetic example baseline

`tests/fixtures/resume/example-render-baseline/` is the checked-in real baseline
for the synthetic `example/` workspace. It is captured by
`tests.resume.render_baseline.capture_example_render_baseline`, which:

1. builds the pinned `docker/render.Dockerfile` image with Podman,
2. renders the tracked synthetic example outputs inside that container,
3. records the full output filename matrix,
4. stores normalized Markdown/HTML/TXT and normalized `pdftotext` output in
   `manifest.json`, and
5. stores 144-DPI page rasters plus SHA-256 hashes for every generated PDF.

The fixture is intentionally separate from the tiny calibration assets above.
Tests fail if the real manifest is missing, still marked synthetic, or if any
recorded raster hash no longer matches its checked-in page artifact.

Current captured container evidence:

- Image ID: `sha256:fc20fc392e84da606250ddf0c36dd5239ab6fdca5ba3573ee7bc4e18f6b7f548`
- Python: `3.12.13`
- Pandoc: `2.17.1.1`
- WeasyPrint: `67.0`
- pdftotext / pdftoppm / pdfinfo: `22.12.0`

## Local evidence captured while authoring U0

The current macOS workstation reported these local tool versions when sampled:

- Pandoc `3.10`
- WeasyPrint `67.0`
- pdftotext `26.07.0`

Those observations are recorded in `docker/render-versions.env` as local
baseline metadata. They are not treated as proof of the Linux container's exact
package build, but they give U0 a reviewed starting point before CI begins
building the dedicated render image.
