---
name: resume-build
description: Build and validate resume variants without editing derived outputs.
---

# Resume Build & Verify

1. Read [ARCHITECTURE.md](../../../ARCHITECTURE.md) and treat Markdown under
   `private/profile/` and `private/companies/` as the source of truth.
2. Build the requested matrix with
   `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> <mode>`.
3. Run `UV_CACHE_DIR=.uv-cache uv run career-resume validate` after source edits.
4. Inspect generated files under `private/build/`; never edit them directly.
5. Report missing sections with source paths and do not embellish or conflate technologies.
