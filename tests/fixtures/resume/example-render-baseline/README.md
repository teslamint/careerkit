# Example render baseline fixtures

These fixtures are the real synthetic-example render baseline captured from the
pinned `docker/render.Dockerfile` environment. They are separate from the tiny
calibration-only fixtures in `tests/fixtures/resume/pdf-baseline/`.

- `manifest.json` records the full example output matrix, normalized text
  outputs, normalized `pdftotext` output, page counts, raster DPI, and the
  container image/tool versions used at capture time.
- `pages/<artifact>/page-*.ppm` stores the 144-DPI raster pages whose hashes are
  referenced by the manifest.

The source data is the checked-in synthetic `example/` workspace only.
