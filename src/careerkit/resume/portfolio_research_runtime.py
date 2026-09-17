"""Uninstalled module runner for private portfolio research validation."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
from collections.abc import Sequence

from careerkit.resume.adapters.portfolio_markdown import load_catalog, load_publication_result
from careerkit.resume.application.portfolio_research import (
    load_stale_disposition_inventory,
    prepare_stale_cleanup_headless,
    validate_research,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--planning-input", type=Path)
    parser.add_argument("--expected-digest")
    parser.add_argument("--cleanup-headless", action="store_true")
    parser.add_argument("checks", nargs="*")
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    source_root = arguments.source_root.resolve() if arguments.source_root else root.parent
    digest_matches = (
        arguments.expected_digest is not None
        and arguments.planning_input is not None
        and arguments.planning_input.is_file()
        and sha256(arguments.planning_input.read_bytes()).hexdigest() == arguments.expected_digest
    )
    if arguments.cleanup_headless:
        if not root.is_relative_to(source_root) or not digest_matches:
            print("cleanup=prepared replacements=0 errors=1")
            return 2
        try:
            _occurrences, logical = load_catalog(root / "catalog.md")
            superseded = load_publication_result(root / "transactions" / "u2-initial.md")
            inventory_path = root / "runtime-evidence" / "U2" / "stale-disposition-inventory.md"
            inventory = load_stale_disposition_inventory(inventory_path)
            prepared = prepare_stale_cleanup_headless(
                root,
                {item.logical_repository_id for item in logical},
                superseded,
                inventory,
                inventory_path,
                root / "runtime-evidence" / "U2" / "stale-cleanup-proposal.md",
            )
        except (OSError, ValueError):
            print("cleanup=prepared replacements=0 errors=1")
            return 2
        print(f"cleanup=prepared replacements={len(prepared.writes)} errors=0")
        return 0
    if not arguments.checks:
        parser.error("at least one validation check is required")
    _validate_checks(parser, arguments.checks)
    validation_digest_matches = arguments.expected_digest is None or digest_matches
    if not root.is_relative_to(source_root) or not validation_digest_matches:
        errors = 1
        warnings = 0
    else:
        issues = validate_research(root, arguments.checks)
        errors = sum(issue.severity == "error" for issue in issues)
        warnings = sum(issue.severity == "warning" for issue in issues)
    check_count = len(arguments.checks) - ("lane" in arguments.checks)
    print(f"checks={check_count} errors={errors} warnings={warnings}")
    return 2 if errors else 0


def _validate_checks(parser: argparse.ArgumentParser, checks: Sequence[str]) -> None:
    allowed = {
        "discovery",
        "logical-repositories",
        "attribution",
        "periods",
        "refresh",
        "lane",
        "dispositions",
        "evidence-shape",
        "privacy",
        "proposals",
        "references",
        "contexts",
    }
    materialized = tuple(checks)
    lane_positions = [index for index, check in enumerate(materialized) if check == "lane"]
    if len(lane_positions) > 1:
        parser.error("lane accepts exactly one selector")
    selector_position: int | None = None
    if lane_positions:
        selector_position = lane_positions[0] + 1
        if selector_position >= len(materialized):
            parser.error("lane requires a selector")
        if materialized[selector_position] not in {
            f"lane-{index}" for index in range(1, 5)
        }:
            parser.error("lane selector must be lane-1 through lane-4")
    for index, check in enumerate(materialized):
        if index == selector_position:
            continue
        if check not in allowed:
            parser.error(f"unknown validation check: {check}")


if __name__ == "__main__":
    raise SystemExit(main())
