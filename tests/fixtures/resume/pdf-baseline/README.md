# Synthetic PDF baseline fixtures

These fixtures are calibration-only assets for U0 PDF equivalence work.
They intentionally avoid any real resume or job-posting content.

- `page-1.ppm` anchors the exact-hash fast path.
- `known-failure-baseline.ppm` and `known-failure-candidate.ppm` prove that a
  threshold breach emits diagnostic copies and an amplified diff image.
- `manifest.json` records the normalized text placeholder, page count, raster
  DPI, and SHA-256 hashes.
