---
name: verify-interview-content
description: Verify interview sheet/mock interview answers against resume source. Use AFTER generating or editing interview preparation documents to catch fabricated claims.
---

# Content Integrity Verification

Verify that interview sheet answers are grounded in the actual resume. Catches when specific technical experiences are fabricated for a company based on inference rather than documented facts.

## When to Use

- After creating or editing files in `private/jd_analysis/interview/`
- After generating mock interview documents
- After editing `resume-based-qa.md`

## Procedure

### Step 0: Refresh the generated verifier config

Before verification, read the current private resume, career, and portfolio sources plus the
target position context. Generate or refresh `private/verify_content_config.json` from those
sources; never copy its private values into tracked files, logs, or replies.

- `company_aliases`: company names and aliases used by the current sources
- `parent_company_map`: source-backed company or product relationships
- `technology_keywords`: position-specific technology claims whose evidence must be checked
- `pattern_keywords`: specific patterns or solution claims that must not be inferred from more
  general experience statements

The keyword lists are user- and position-specific generated inputs. Do not restore a package-wide
hardcoded keyword catalog or assume that one candidate's disallowed claims apply to another.

### Step 1: Run automated verification

```bash
UV_CACHE_DIR=.uv-cache uv run career-resume verify-content private/jd_analysis/interview/<interview-file>.md
```

If `--resume` is not specified, defaults to `private/build/resume-job-base.md`.
Use `--json` for machine-readable output (CI integration).

**Note**: Only `> ` (blockquote) lines are scanned for claims. Non-blockquote text is ignored.

### Step 2: Review results

- **✅ verified**: Claim found in correct company section of resume — OK
- **⚠️ uncertain**: Claim found in a different company section — check if company attribution is correct
- **❌ ungrounded**: Claim not found anywhere in resume — **must fix or remove**

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

Confirm 0 ungrounded claims.

## Key Rule

**"Used X technology" ≠ "Had specific experience Y with X"**

Examples:
- "Used Spring Boot" ≠ "Solved JPA N+1 with fetch join"
- "Commerce service" ≠ "Designed product-category entity relationships"
- "Used JPA" ≠ "Applied DTO projection with QueryDSL"
