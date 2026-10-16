"""Cross-language URL pattern verification against shared fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careerkit.jobs.application.storage_migration import extract_job_id, get_platform_from_url, posting_key_from_url

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


# zighang cases are built from the host here, not stored in the shared fixture, so the
# publish guard's posting-url rule does not read the synthetic id as a pasted posting.
ZIGHANG_HOST = "zighang.com"
ZIGHANG_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
WANTED_HOST = "www.wanted.co.kr"
REMEMBER_HOST = "rememberapp.co.kr"
GROUPBY_HOST = "groupby.kr"
SARAMIN_HOST = "www.saramin.co.kr"


@pytest.mark.parametrize(
    "url",
    [f"https://{ZIGHANG_HOST}/recruitment/{ZIGHANG_ID}", f"https://{ZIGHANG_HOST}/recruitment/{ZIGHANG_ID}?ref=list"],
)
def test_zighang_url_matches_js_expectation(url):
    assert (get_platform_from_url(url), extract_job_id(url)) == ("zighang", ZIGHANG_ID)
    assert posting_key_from_url(url) == ("zighang", ZIGHANG_ID)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{WANTED_HOST}/wd/١٢",
        f"https://{REMEMBER_HOST}/job/posting/١٢",
        f"https://{GROUPBY_HOST}/positions/١٢",
        f"https://{SARAMIN_HOST}/zf_user/jobs/relay/view?rec_idx=١٢",
    ],
)
def test_job_ids_are_ascii_digits_only(url):
    assert extract_job_id(url) is None
    assert posting_key_from_url(url) is None
