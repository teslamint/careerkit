"""Cross-language URL pattern verification against shared fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careerkit.jobs.application.storage_migration import extract_job_id, get_platform_from_url

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "url_patterns.json"


def _load_fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(params=_load_fixtures(), ids=lambda f: f["url"])
def url_fixture(request):
    return request.param


def test_url_pattern_matches_js_expectation(url_fixture):
    """Cross-validate positive matches between Python backend and JS detector.

    Negative cases (platform=null) are JS-only — the backend may recognize
    platforms the extension intentionally doesn't support in v1 (e.g. jumpit).
    """
    url = url_fixture["url"]
    expected_platform = url_fixture["platform"]
    expected_job_id = url_fixture["job_id"]

    if expected_platform is None:
        pytest.skip("negative case — JS detector only")

    if url_fixture.get("note", "").startswith("cross-platform"):
        pytest.skip("JS-only: tests anchored parsing not applied to storage_migration.py")

    actual_job_id = extract_job_id(url)
    actual_platform = get_platform_from_url(url)

    assert actual_job_id == expected_job_id, (
        f"job_id mismatch for {url}: expected {expected_job_id}, got {actual_job_id}"
    )
    assert actual_platform == expected_platform, (
        f"platform mismatch for {url}: expected {expected_platform}, got {actual_platform}"
    )
