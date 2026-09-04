---
name: resume-build
description: Build and validate resume variants without editing derived outputs.
---

# Resume Build & Verify

1. Read [ARCHITECTURE.md](../../../ARCHITECTURE.md) and treat Markdown under
   `private/profile/` and `private/companies/` as the source of truth.
2. Read `private/portfolio-research/AGENTS.md` when it exists. Follow its evidence ID links and omit concrete claims without supporting evidence IDs.
3. Build the requested matrix with
   `UV_CACHE_DIR=.uv-cache uv run career-resume build <variant> <mode>`.
4. Run `UV_CACHE_DIR=.uv-cache uv run career-resume validate` after source edits.
5. Inspect generated files under `private/build/`; never edit them directly.
6. Report missing sections with source paths and do not embellish or conflate technologies.
