from __future__ import annotations

from .active_surfaces import (
    HISTORICAL_FILES,
    HISTORICAL_ROOTS,
    derive_active_surfaces,
    load_active_surface_manifest,
)


def test_active_surface_manifest_matches_derived_tracked_set() -> None:
    assert load_active_surface_manifest() == derive_active_surfaces()


def test_historical_exclusions_are_explicit_and_narrow() -> None:
    assert HISTORICAL_ROOTS == (
        ".claude/plans/",
        ".entire/",
        ".entirecontext/",
        "docs/plans/",
        "docs/research/",
        "docs/superpowers/",
    )
    assert HISTORICAL_FILES == {"CHANGELOG.md", "LESSONS.md", "LICENSE"}
