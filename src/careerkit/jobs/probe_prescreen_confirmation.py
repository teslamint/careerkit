"""Probe: does a JD body cancel a title-only pre-screen cut, on the live corpus?

Read-only apart from the repository's own lock files: it derives the disputed set at
runtime — every stored record the title filter cuts whose title nonetheless carries a
backend token — and reports what `_pre_screen_reason` returns for each, alongside the
requirement-manifest evidence that drove it. No record content, manifest, or counter
changes.

No record identifier is embedded. The set is whatever the live corpus and the live
config produce today, so a corpus that moves or a config that drifts changes the
derived set rather than silently passing a stale roster.

The invariant this probe fails on: a record whose requirement manifest contains a
backend match must not still carry a title-inferred reason. `closed` and
`prior_application` are exempt — they rest on evidence rather than on a guess about
the role, and the confirmation never cancels them.

A record whose manifest is empty keeps its reason by design: an unparseable body is
not a match, so the title decision stands. That is reported, not failed.

Run:
    uv run python -m careerkit.jobs.probe_prescreen_confirmation
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping, Sequence

import yaml

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.automation import _pre_screen_reason
from careerkit.jobs.application.requirement_manifest import RequirementItem, extract_requirement_manifest
from careerkit.jobs.application.title_filter import has_backend_keyword, quick_filter_title
from careerkit.workspace import resolve_workspace

TRUNCATE = 60

# Reasons the confirmation never cancels, so a record carrying one is not evidence
# against the mechanism.
EVIDENCE_REASONS = ("closed", "prior_application")


def load_quick_filters(workspace: Any) -> dict[str, Any]:
    """Load the live `quick_filters` mapping the pipeline itself passes.

    Raises rather than falling back to an empty mapping. An empty exclusion list
    makes every record confirm, which would let this probe report success while the
    filter under test never ran.
    """
    config_path = workspace.jobs_config_dir / "search_config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise SystemExit(f"configuration is not a mapping in {config_path}")
    quick_filters = config.get("quick_filters")
    if not isinstance(quick_filters, Mapping):
        raise SystemExit(f"quick_filters missing or not a mapping in {config_path}")
    title_exclude = quick_filters.get("title_exclude")
    # A bare string is truthy and iterable, so a plain truthiness check would accept
    # `title_exclude: Backend` and then match character by character.
    if not (
        isinstance(title_exclude, list)
        and title_exclude
        and all(isinstance(value, str) and value.strip() for value in title_exclude)
    ):
        raise SystemExit(
            f"quick_filters.title_exclude in {config_path} must be a non-empty list of "
            "non-blank strings; nothing to measure otherwise"
        )
    return dict(quick_filters)


def disputed_titles(repository: JDRecordRepository, quick_filters: Mapping[str, Any]) -> list[Any]:
    """Records the title filter cuts whose title still carries a backend token.

    This is the population the requirement confirmation exists to settle.
    """
    cfg = {"quick_filters": quick_filters}
    disputed = []
    for item in repository.list_metadata():
        title = item.record.position or ""
        if not title:
            continue
        if quick_filter_title(title, cfg) != "pass":
            continue
        if not has_backend_keyword(title):
            continue
        disputed.append(item.record.key)
    return disputed


def matching_parents(jd_markdown: str) -> tuple[int, list[RequirementItem]]:
    """Parent count and the items that matched, for the explanatory output column.

    Scans `items`, the same set `requirements_show_backend` decides on. The parent
    count stays the readability signal — 0 parents means an unparseable body — but
    the matches must come from the set the verdict came from, or the column explains
    a decision the code did not make.
    """
    manifest = extract_requirement_manifest(jd_markdown)
    return len(manifest.parents), [item for item in manifest.items if has_backend_keyword(item.text)]


def format_matches(matches: Sequence[RequirementItem]) -> str:
    if not matches:
        return "-"
    return "; ".join(
        f"[{item.kind}] {item.text[:TRUNCATE]}" + ("…" if len(item.text) > TRUNCATE else "")
        for item in matches
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-disputed",
        type=int,
        default=None,
        help="fail when the derived disputed set is not this size (guards against corpus drift)",
    )
    args = parser.parse_args(argv)

    workspace = resolve_workspace()
    repository = JDRecordRepository(workspace.jobs_records_dir)
    quick_filters = load_quick_filters(workspace)

    keys = disputed_titles(repository, quick_filters)
    if not keys:
        raise SystemExit("derived disputed set is empty; the title filter or the corpus changed")
    if args.expect_disputed is not None and len(keys) != args.expect_disputed:
        raise SystemExit(f"derived {len(keys)} disputed records, expected {args.expect_disputed}")

    confirmed = 0
    empty_manifest = 0
    non_backend = 0
    evidence_reason = 0
    failures: list[str] = []

    print(f"{'key':17} {'reason':16} {'parents':7} {'hits':5} outcome / matched parents")
    for key in keys:
        record = repository.get(key)
        # Empty prior_records is deliberate, not a shortcut: passing the real prior set
        # would let `prior_application` fire and mask the title-versus-body decision this
        # probe exists to measure. Do not "fix" this by loading the store's records.
        reason = _pre_screen_reason(record, [], quick_filters)
        parents, matches = matching_parents(record.jd_markdown)
        name = f"{key.platform}:{key.job_id}"
        shown = reason if reason is not None else "None"

        if reason is None:
            confirmed += 1
            outcome = "CONFIRMED"
        elif reason in EVIDENCE_REASONS:
            evidence_reason += 1
            outcome = f"EXEMPT — {reason} rests on evidence, never cancelled"
        elif matches:
            outcome = "FAIL — manifest matched but the title reason stands"
            failures.append(f"{name}: {reason} with {len(matches)} matching parent(s)")
        elif parents == 0:
            empty_manifest += 1
            outcome = "SET ASIDE — empty manifest, title decision stands"
        else:
            non_backend += 1
            outcome = "SET ASIDE — requirements carry no backend signal"

        print(f"{name:17} {shown:16} {parents:<7} {len(matches):<5} {outcome}; {format_matches(matches)}")

    print()
    print(f"disputed records derived: {len(keys)}")
    print(f"  confirmed by requirements: {confirmed}")
    print(f"  set aside, empty manifest: {empty_manifest}")
    print(f"  set aside, non-backend requirements: {non_backend}")
    print(f"  exempt (closed / prior_application): {evidence_reason}")
    print(f"failures: {len(failures)}")
    for line in failures:
        print(f"  {line}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
