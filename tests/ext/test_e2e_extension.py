"""Playwright E2E coverage for the real browser extension side panel.

These tests load the shipped extension in a persistent Chromium context and then
mock only the browser/native boundary (`chrome.tabs.query`,
`chrome.runtime.sendMessage`, `chrome.runtime.onMessage`) from inside the real
`chrome-extension://.../sidepanel/panel.html` page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = pw.sync_playwright
expect = pw.expect

_EXT_PATH = Path(__file__).parents[2] / "ext"
_TARGET_URL = "https://www.wanted.co.kr/wd/123456"
_RERUN_COMMAND = (
    "UV_CACHE_DIR=/private/tmp/uv-cache uv run pytest -q tests/ext/test_e2e_extension.py"
)
_SCREENING_MARKDOWN = """## 요약
### 최종 판정
추천
## 핵심 근거
- 경력 요건과 기술 스택이 맞습니다.
| 항목 | 값 |
| --- | --- |
| 경력 | 5년 |
| 언어 | Python |
"""
_JD_MARKDOWN = """# JD 원문
백엔드 엔지니어 채용
"""
_COMPANY_MARKDOWN = """## 회사 정보
- 원격 협업 문화가 있습니다.
| 항목 | 값 |
| --- | --- |
| 설립 | 2020 |
| 인원 | 42 |
"""

_PANEL_TEST_INIT_SCRIPT = r"""
(state) => {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const internal = {
    activeTabUrl: state.activeTabUrl,
    responses: clone(state.responses || {}),
    requests: [],
    listeners: [],
  };

  const nextResponse = (action) => {
    const queue = internal.responses[action];
    if (!Array.isArray(queue) || queue.length === 0) {
      return { status: "ok", data: null };
    }
    return queue.shift();
  };

  window.__panelTest = {
    emit(message) {
      internal.listeners.forEach((listener) => listener(message, {}, () => {}));
    },
    getRequests() {
      return clone(internal.requests);
    },
  };

  chrome.tabs.query = (_query, callback) => {
    callback([{ id: 1, active: true, url: internal.activeTabUrl }]);
  };

  chrome.runtime.sendMessage = (request, callback) => {
    internal.requests.push(clone(request));
    if (callback) callback(nextResponse(request.action));
  };

  chrome.runtime.onMessage.addListener = (listener) => {
    internal.listeners.push(listener);
  };

}
"""


@pytest.fixture
def extension_context(tmp_path: Path) -> Iterator[Any]:
    profile_dir = tmp_path / "chromium-profile"
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chromium",
                headless=True,
                args=[
                    f"--disable-extensions-except={_EXT_PATH}",
                    f"--load-extension={_EXT_PATH}",
                    "--no-first-run",
                ],
            )
        except Exception as exc:
            pytest.fail(
                "Headless Chromium extension launch failed. "
                f"If this is sandbox-blocked, rerun with escalation: {_RERUN_COMMAND}. "
                f"Original error: {exc}"
            )
        try:
            yield context
        finally:
            context.close()


@pytest.fixture
def extension_id(extension_context: Any) -> str:
    if extension_context.service_workers:
        worker = extension_context.service_workers[0]
    else:
        worker = extension_context.wait_for_event("serviceworker", timeout=10_000)
    return worker.url.split("/")[2]


class PanelHarness:
    def __init__(
        self,
        extension_context: Any,
        extension_id: str,
        responses: dict[str, list[dict[str, Any]]],
        *,
        viewport: tuple[int, int] = (1280, 900),
        color_scheme: str | None = None,
        active_tab_url: str = _TARGET_URL,
    ) -> None:
        self.page = extension_context.new_page()
        self.errors: list[str] = []
        self.page.on(
            "console",
            lambda msg: self.errors.append(f"console:{msg.text}") if msg.type == "error" else None,
        )
        self.page.on("pageerror", lambda exc: self.errors.append(f"page:{exc}"))
        self.page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
        if color_scheme:
            self.page.emulate_media(color_scheme=color_scheme)
        state = json.dumps(
            {"activeTabUrl": active_tab_url, "responses": responses},
            ensure_ascii=False,
        )
        self.page.add_init_script(script=f"({_PANEL_TEST_INIT_SCRIPT})({state})")
        self.page.goto(f"chrome-extension://{extension_id}/sidepanel/panel.html")

    def emit(self, message: dict[str, Any]) -> None:
        self.page.evaluate("(payload) => window.__panelTest.emit(payload)", message)

    def requests(self) -> list[dict[str, Any]]:
        return self.page.evaluate("() => window.__panelTest.getRequests()")

    def assert_no_errors(self) -> None:
        assert self.errors == []



def _detail_response(
    *,
    verdict: str = "recommended",
    verdict_capped: bool = False,
    screening_markdown: str = _SCREENING_MARKDOWN,
    jd_markdown: str = _JD_MARKDOWN,
    provider: str = "openai",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "data": {
            "record": {
                "company": "Acme Labs",
                "position": "Backend Engineer",
                "screening_verdict": verdict,
                "verdict_capped": verdict_capped,
                "screening_provider": provider,
            },
            "screening_markdown": screening_markdown,
            "jd_markdown": jd_markdown,
            "is_fallback": False,
        },
    }


@pytest.mark.parametrize("color_scheme", [None, "dark"])
def test_extension_loads_service_worker(extension_context: Any, extension_id: str, color_scheme: str | None) -> None:
    assert re.fullmatch(r"[a-p]{32}", extension_id)
    page = extension_context.new_page()
    errors: list[str] = []
    page.on(
        "console",
        lambda msg: errors.append(f"console:{msg.text}") if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: errors.append(f"page:{exc}"))
    if color_scheme:
        page.emulate_media(color_scheme=color_scheme)
    page.goto(f"chrome-extension://{extension_id}/sidepanel/panel.html")
    expect(page.locator(".state-title")).to_have_text("채용 사이트에서 사용하세요")
    assert errors == []
    page.close()


def test_collect_flow_shows_company_info_stages_before_screening_and_refreshes_company_tab(
    extension_context: Any,
    extension_id: str,
) -> None:
    harness = PanelHarness(
        extension_context,
        extension_id,
        responses={
            "get_detail": [
                {"status": "ok", "data": {"record": None, "screening_markdown": None, "jd_markdown": None, "is_fallback": False}},
                _detail_response(),
            ],
            "collect": [{"status": "accepted"}],
            "get_company_info": [
                {"status": "ok", "data": {"company_info_markdown": _COMPANY_MARKDOWN}},
            ],
        },
    )

    expect(harness.page.locator(".collect-button")).to_have_text("공고 수집하기")
    harness.page.locator(".collect-button").click()
    expect(harness.page.locator("#progress-stage")).to_have_text("추출 중...")

    harness.emit({"action": "screening_progress", "url": _TARGET_URL, "stage": "company_info", "state": "checking"})
    expect(harness.page.locator("#progress-stage")).to_have_text("회사정보 확인 중...")

    harness.emit({"action": "screening_progress", "url": _TARGET_URL, "stage": "company_info", "state": "enriching"})
    expect(harness.page.locator("#progress-stage")).to_have_text("회사정보 보강 중...")

    harness.emit({"action": "screening_progress", "url": _TARGET_URL, "stage": "screening", "state": "running"})
    expect(harness.page.locator("#progress-stage")).to_have_text("스크리닝 중...")

    harness.emit(
        {
            "action": "screening_complete",
            "url": _TARGET_URL,
            "data": {
                "company_info": {
                    "status": "ready",
                    "attempted": True,
                    "persisted": True,
                    "completeness": 88,
                    "warning_code": None,
                }
            },
        }
    )

    expect(harness.page.locator(".company-info-notice-title")).to_have_text("회사 정보 준비 완료")
    expect(harness.page.locator(".company-info-notice-body")).to_have_text("완성도 88%")
    expect(harness.page.locator(".company-name")).to_have_text("Acme Labs")
    expect(harness.page.locator(".tab-btn[data-tab='screening']")).to_have_text("스크리닝")
    expect(harness.page.locator(".tab-btn[data-tab='jd']")).to_have_text("JD 원문")
    expect(harness.page.locator(".tab-btn[data-tab='company']")).to_have_text("회사 정보")
    expect(
        harness.page.locator(".tab-content[data-tab='screening'] .table-wrap table")
    ).to_have_count(1)

    harness.page.locator(".tab-btn[data-tab='jd']").click()
    expect(harness.page.locator(".tab-content[data-tab='jd'] pre")).to_contain_text("백엔드 엔지니어 채용")

    company_button = harness.page.locator(".tab-btn[data-tab='company']")
    expect(company_button).to_be_enabled()
    company_button.click()
    expect(harness.page.locator(".tab-content[data-tab='company']")).to_contain_text("원격 협업 문화가 있습니다.")
    expect(harness.page.locator(".tab-content[data-tab='company'] .table-wrap table")).to_have_count(1)
    assert [request["action"] for request in harness.requests()] == [
        "get_pending_status",
        "get_detail",
        "collect",
        "get_detail",
        "get_company_info",
    ]
    harness.assert_no_errors()
    harness.page.close()


def test_below_threshold_warning_keeps_verdict_visible_and_works_at_375px_in_dark_mode(
    extension_context: Any,
    extension_id: str,
) -> None:
    harness = PanelHarness(
        extension_context,
        extension_id,
        responses={
            "get_detail": [
                _detail_response(verdict="hold", verdict_capped=True),
                _detail_response(verdict="hold", verdict_capped=True),
            ],
            "get_company_info": [
                {"status": "ok", "data": {"company_info_markdown": _COMPANY_MARKDOWN}},
                {"status": "ok", "data": {"company_info_markdown": _COMPANY_MARKDOWN}},
            ],
        },
        viewport=(375, 812),
        color_scheme="dark",
    )

    harness.emit(
        {
            "action": "screening_complete",
            "url": _TARGET_URL,
            "data": {
                "company_info": {
                    "status": "warning",
                    "attempted": True,
                    "persisted": True,
                    "completeness": 62,
                    "warning_code": "below_threshold",
                }
            },
        }
    )

    expect(harness.page.locator(".company-info-notice-title")).to_have_text("회사 정보 보강 필요")
    expect(harness.page.locator(".company-info-notice-body")).to_have_text("완성도 62%")
    expect(harness.page.locator(".verdict-label")).to_have_text("대기")
    expect(harness.page.locator(".capped-title")).to_have_text("⚠ 로컬 모델 판정")
    expect(harness.page.locator(".rescreen-btn")).to_have_text("재스크리닝")
    expect(
        harness.page.locator(".tab-content[data-tab='screening'] .table-wrap table")
    ).to_have_count(1)

    viewport_width = harness.page.evaluate("() => document.documentElement.clientWidth")
    dark_bg = harness.page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
    )
    body_bg = harness.page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert viewport_width == 375
    assert dark_bg == "#111827"
    assert body_bg == "rgb(17, 24, 39)"

    company_button = harness.page.locator(".tab-btn[data-tab='company']")
    expect(company_button).to_be_enabled()
    company_button.click()
    expect(harness.page.locator(".tab-content[data-tab='company']")).to_contain_text("설립")
    harness.assert_no_errors()
    harness.page.close()


def test_missing_company_info_warning_does_not_use_failure_styling(
    extension_context: Any,
    extension_id: str,
) -> None:
    harness = PanelHarness(
        extension_context,
        extension_id,
        responses={
            "get_detail": [_detail_response(), _detail_response()],
        },
    )

    harness.emit(
        {
            "action": "screening_complete",
            "url": _TARGET_URL,
            "data": {
                "company_info": {
                    "status": "warning",
                    "attempted": True,
                    "persisted": False,
                    "completeness": None,
                    "warning_code": "missing",
                }
            },
        }
    )

    expect(harness.page.locator(".company-info-notice-title")).to_have_text("회사 정보가 아직 없습니다")
    expect(harness.page.locator(".company-info-notice-body")).to_have_text(
        "플랫폼 정보만으로 스크리닝을 계속했습니다."
    )
    expect(harness.page.locator(".verdict-label")).to_have_text("추천")
    assert harness.page.locator(".company-info-notice-body").text_content() == "플랫폼 정보만으로 스크리닝을 계속했습니다."
    assert harness.page.locator(".state-error").count() == 0
    assert harness.page.locator(".tab-btn[data-tab='company']").is_disabled()
    assert [request["action"] for request in harness.requests()] == [
        "get_pending_status",
        "get_detail",
        "get_company_info",
        "get_detail",
    ]
    harness.assert_no_errors()
    harness.page.close()
