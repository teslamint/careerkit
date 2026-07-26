"""Playwright E2E tests for the CareerKit browser extension: extension load
(service worker) and side panel rendering.

Chrome extensions require a headed browser (MV3 does not run under
`headless=True`), so these tests launch a persistent context with
`headless=False` and skip gracefully wherever that isn't possible: no
Playwright install, no browser binary, CI/headless-only environments.

Native Messaging host integration (connection errors, live screening detail)
is out of scope here — the side panel resolves the "current job posting" via
`chrome.tabs.query` on its own tab, and a panel opened as a top-level tab is
itself the active tab (a chrome-extension:// URL), so it can never be made to
look like a recruitment-site tab from here. Process-level native host
behavior is already covered by tests/ext/test_integration.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = pw.sync_playwright
expect = pw.expect

_EXT_PATH = Path(__file__).parents[2] / "ext"

pytestmark = pytest.mark.skipif(
    os.environ.get("CI") is not None,
    reason="extension E2E requires a headed browser, unsupported in CI",
)


@pytest.fixture
def extension_context(tmp_path: Path) -> Iterator[Any]:
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(tmp_path / "chrome-profile"),
                headless=False,
                args=[
                    f"--disable-extensions-except={_EXT_PATH}",
                    f"--load-extension={_EXT_PATH}",
                    "--no-first-run",
                ],
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"cannot launch headed Chromium for extension E2E: {exc}")
        try:
            yield context
        finally:
            context.close()


def _extension_id(context: Any) -> str:
    for sw in context.service_workers:
        if sw.url.startswith("chrome-extension://"):
            return sw.url.split("/")[2]
    sw = context.wait_for_event("serviceworker", timeout=10_000)
    return sw.url.split("/")[2]


def test_extension_loads_service_worker(extension_context: Any) -> None:
    ext_id = _extension_id(extension_context)
    assert re.fullmatch(r"[a-p]{32}", ext_id)


def test_side_panel_loads_and_shows_non_recruitment_state(
    extension_context: Any,
) -> None:
    ext_id = _extension_id(extension_context)
    page = extension_context.new_page()
    page.goto(f"chrome-extension://{ext_id}/sidepanel/panel.html")

    expect(page.locator(".state-title")).to_have_text("채용 사이트에서 사용하세요")
    expect(page.locator(".state-body")).to_contain_text("지원 중인 채용 공고 페이지를 열면")
    page.close()


def test_side_panel_renders_without_console_errors(
    extension_context: Any,
) -> None:
    ext_id = _extension_id(extension_context)
    page = extension_context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"chrome-extension://{ext_id}/sidepanel/panel.html")
    page.wait_for_selector(".state-title")

    assert errors == []
    page.close()


def test_content_script_no_context_invalidation_after_reload(
    extension_context: Any,
) -> None:
    """After extension reload, old content script intervals must not throw
    'Extension context invalidated'. The new content script should inject
    cleanly on page refresh."""
    ext_id = _extension_id(extension_context)
    page = extension_context.new_page()
    page.goto("https://www.wanted.co.kr/", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_timeout(2000)

    page.goto("chrome://extensions", wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    # Trigger extension reload via the Chrome extensions page
    # Find the reload button for our extension
    reload_btn = page.locator(
        f'extensions-manager').first
    if reload_btn.count() > 0:
        # Use JS to reload the extension via chrome.management API
        sw_page = extension_context.new_page()
        sw_page.goto(f"chrome-extension://{ext_id}/sidepanel/panel.html")
        sw_page.evaluate("chrome.runtime.reload()")
        sw_page.close()
    else:
        pytest.skip("cannot locate extension reload mechanism")

    page.wait_for_timeout(1000)

    # Navigate to a wanted page and verify no "Extension context invalidated" errors
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto("https://www.wanted.co.kr/", wait_until="domcontentloaded", timeout=15_000)
    page.wait_for_timeout(3000)

    context_errors = [e for e in errors if "Extension context invalidated" in e]
    assert context_errors == [], f"Context invalidation errors found: {context_errors}"
    page.close()
