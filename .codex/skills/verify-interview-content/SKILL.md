---
name: verify-interview-content
description: Verify generated documents against resume sources and configured forbidden terms.
---

# Content Integrity Verification

Verify generated document claims are grounded in the actual resume and that removed claims do not reappear. Catches fabricated experiences and explicitly forbidden terms.

## When to Use

- After creating or editing any generated resume, career description, report, packet note, interview sheet, or mock interview

## Procedure

Read `private/portfolio-research/AGENTS.md` when it exists. Follow its evidence ID links. Omit concrete claims that lack supporting evidence IDs.

For each concrete claim, resolve every referenced ID at
`private/portfolio-research/evidence/<evidence-id>.md` and confirm that the record supports the
claim. A keyword match alone is insufficient. Treat a missing, unknown, or mismatched evidence ID
as an ungrounded claim.

### Step 0: Refresh the generated verifier config

Before verification, read the current private resume, career, and portfolio sources plus the
target position context. Generate or refresh `private/verify_content_config.json` from those
sources; never copy its private values into tracked files, logs, or replies.

- `company_aliases`: company names and aliases used by the current sources
- `parent_company_map`: source-backed company or product relationships
- `technology_keywords`: position-specific technology claims whose evidence must be checked
- `pattern_keywords`: specific patterns or solution claims that must not be inferred from more
  general experience statements
- `forbidden_terms`: previously removed claims or phrases that must not reappear

The keyword lists are user- and position-specific generated inputs. Do not restore a package-wide
hardcoded keyword catalog or assume that one candidate's disallowed claims apply to another.

### Step 1: Run automated verification

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume verify-content <document>
```

By default, only `> ` (blockquote) lines are scanned for claims. Use `--all-lines` for other generated documents. Forbidden terms are always checked on every line. Use `--json` for machine-readable output; it includes a `forbidden` array.

### Step 2: Review results

- **✅ verified**: Claim found in the correct company section and supported by a resolved evidence ID.
  Return ✅ verified only after the final result states `ungrounded claims: 0`.
- **⚠️ uncertain**: Claim found in a different company section — check if company attribution is correct
- **❌ ungrounded**: Claim not found anywhere in resume, or its evidence ID is missing, unknown, or mismatched — **must fix or remove**

### Step 3: Fix ungrounded claims

For each ❌ ungrounded claim:

1. Read the resume section for that company (`private/build/resume-job-base.md`)
2. If the claim is genuinely in the resume but the keyword didn't match → false positive, ignore
3. If the claim is fabricated (inferred from technology stack, not documented) → fix:
   - Replace with what the resume actually says
   - Or rewrite as "직접 경험은 없지만 ~로 접근하겠다"
   - Add `해설` note that this is a gap area requiring pre-interview study

### Step 4: Re-run verification

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume verify-content <interview-file>.md
```

State `ungrounded claims: 0` explicitly. Do not return ✅ verified while any ungrounded claim remains.

## Key Rule

**"Used X technology" ≠ "Had specific experience Y with X"**

Examples:
- "Used Spring Boot" ≠ "Solved JPA N+1 with fetch join"
- "Commerce service" ≠ "Designed product-category entity relationships"
- "Used JPA" ≠ "Applied DTO projection with QueryDSL"
