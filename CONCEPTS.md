# Concepts

Canonical vocabulary for this repository. One term per concept; add sparingly.

## Repository artifacts

- **Tracked artifact** — any file committed to this repository. The remote is public, so tracked is equivalent to published: untracking or rewriting history does not unpublish what was pushed. Job-search data (platform job IDs, company and team names, employment types, screening outcomes) never becomes one.

## JD record storage

- **Canonical record** — the single authoritative JD record keyed by `(platform, job_id)`, accessed only through `JDRecordRepository`. *Avoid: "JD file", direct paths under `private/jd/records/`.*
- **Content revision** — the stored JD/screening document snapshot inside a canonical record. The store is **latest-only**: publishing a new revision deletes the previous one; it is not an append-only history.
- **Screening verdict** — the record's metadata judgment (`recommended` / `hold` / `not_recommended`). Lives independently of the screening document text and can disagree with it after metadata-only updates.

## Screening providers

- **Provider chain** — the ordered screening attempt sequence claude → codex → local (Ollama native or OpenAI-compatible via `LOCAL_LLM_BASE_URL`). All attempted providers' errors aggregate into one failure.
- **Fallback document** — the auto-generated 지원 보류 screening written only when every provider fails; distinct from a real screening produced by any provider.
- **Verdict cap** — the ceiling a local provider's verdict is held to (`지원 보류`). A capped record is recoverable: it is queued for rescreening once a stronger provider is available. *Avoid: "downgrade" for this — that word belongs to the gate.*

## Screening document contract

- **Requirement manifest** — the publish-time, code-owned list of JD requirements. It preserves each source section and prevents providers from adding, omitting, or reclassifying rows.
- **Decisive requirement** — a qualification whose source text explicitly says `필수`, `반드시`, `must`, `required`, or `mandatory`. A qualification-section heading alone does not make an item decisive.
- **Output contract** — the machine-readable shape the screening prompt requires, principally the `| 요건 | 구분 | 대조 | 근거 |` matching table with fixed enumerated values. A document violating it takes the retry path rather than being published.
- **Consistency gate** — the publish-time check that lowers a `지원 추천` whose own matching table reports enough unmet 필수 requirements. It reads the model's labels, so it detects self-contradiction, not fabrication.
- **Evidence check** — the provider-independent check reading the filesystem and the résumé corpus directly: cited `[source:]` paths must resolve, and a `충족` row must name at least one technology the résumé actually contains. Detects fabrication, which the consistency gate cannot.
- **Self-contradiction** — a screening document whose matching table and final verdict disagree. The failure mode behind incident A. *Distinct from fabrication, where the table itself is false.*
