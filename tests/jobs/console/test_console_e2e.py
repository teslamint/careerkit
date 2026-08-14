"""Playwright E2E tests for the JD Console frontend."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Iterator

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
Browser = pw.Browser
Page = pw.Page
expect = pw.expect
sync_playwright = pw.sync_playwright

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.application.pipeline import JobsPipelineService
from careerkit.jobs.console.server import create_server
from careerkit.jobs.domain.model import (
    ApplicationEvent,
    ApplicationStatus,
    JobKey,
    JobRecord,
    PostingStatus,
    ScreeningVerdict,
)


def _pipeline_service(repository: JDRecordRepository, tmp_path: Path) -> JobsPipelineService:
    return JobsPipelineService(
        workspace_root=tmp_path,
        repository=repository,
        runtime_dir=tmp_path / "runtime",
    )


@contextmanager
def _running_server(tmp_path: Path) -> Iterator[str]:
    records = tmp_path / "records"
    repo = JDRecordRepository(records)
    for i in range(55):
        repo.create(
            JobRecord(
                platform="wanted",
                job_id=str(1000 + i),
                company=f"Company{i}",
                position=f"Backend Engineer #{i}",
                source_url=f"https://www.wanted.co.kr/wd/{1000 + i}",
                screening_verdict=(
                    ScreeningVerdict.RECOMMENDED if i % 4 == 0
                    else ScreeningVerdict.HOLD if i % 4 == 1
                    else ScreeningVerdict.NOT_RECOMMENDED if i % 4 == 2
                    else None
                ),
                posting_status=PostingStatus.CLOSED if i % 5 == 0 else PostingStatus.ACTIVE,
                application_status=ApplicationStatus.APPLIED if i == 0 else ApplicationStatus.PENDING,
            ),
            jd_markdown=f"# JD for position {i}\n\nJob description content.",
        )
    repo.update_screening_result(
        JobKey("wanted", "1000"),
        screening_markdown="# Screening\n\n| 요건 | 대조 |\n|---|---|\n| 경력 5년 | 충족 |",
    )
    repo.create(
        JobRecord(
            platform="wanted",
            job_id="9999",
            company="CappedCo",
            position="Capped Engineer",
            source_url="https://www.wanted.co.kr/wd/9999",
            screening_verdict=ScreeningVerdict.HOLD,
            verdict_capped=True,
            screening_provider="ollama",
        ),
        jd_markdown="# Capped JD",
    )
    repo.update_screening_result(
        JobKey("wanted", "9999"),
        screening_markdown="# Capped screening\nhold (capped)",
    )
    repo.create(
        JobRecord(
            platform="wanted",
            job_id="8888",
            company="SetAsideCo",
            position="Set Aside Engineer",
            source_url="https://www.wanted.co.kr/wd/8888",
        ),
        jd_markdown="# Set aside JD",
    )
    repo.update_prescreen(JobKey("wanted", "8888"), "title_exclude")
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repo, tmp_path),
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with _running_server(tmp_path_factory.mktemp("console_e2e")) as url:
        yield url


@pytest.fixture(scope="module")
def browser_instance() -> Iterator[Browser]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def browser_page(server_url: str, browser_instance: Browser) -> Iterator[Page]:
    context = browser_instance.new_context()
    page = context.new_page()
    page.goto(server_url)
    try:
        yield page
    finally:
        context.close()


@dataclass(frozen=True)
class MutableConsole:
    page: Page
    repository: JDRecordRepository
    url: str


@pytest.fixture
def mutable_console(tmp_path: Path, browser_instance: Browser) -> Iterator[MutableConsole]:
    records = tmp_path / "records"
    repository = JDRecordRepository(records)
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="42",
            company="Example",
            position="Backend Engineer",
            source_url="https://www.wanted.co.kr/wd/42",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="2026-07-30T09:00:00+09:00",
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at="2026-07-30T09:00:00+09:00",
                    note=None,
                ),
            ),
        ),
        jd_markdown="# JD\n<script>alert(1)</script>",
    )
    repository.create(
        JobRecord(
            platform="wanted",
            job_id="41",
            company="Legacy",
            position="Legacy Engineer",
            source_url="https://www.wanted.co.kr/wd/41",
            application_status=ApplicationStatus.APPLIED,
            application_status_updated_at="2026-07-31T09:00:00+09:00",
            application_history=(
                ApplicationEvent(
                    status=ApplicationStatus.APPLIED,
                    occurred_at="2026-07-31T09:00:00+09:00",
                    note=None,
                ),
            ),
        ),
        jd_markdown="# Legacy JD",
    )
    legacy_manifest = records / "wanted" / "41" / "record.json"
    legacy_payload = json.loads(legacy_manifest.read_text(encoding="utf-8"))
    legacy_payload["schema_version"] = 1
    legacy_payload["record"]["schema_version"] = 1
    legacy_payload["record"].pop("application_history", None)
    legacy_manifest.write_text(json.dumps(legacy_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
        pipeline_service=_pipeline_service(repository, tmp_path),
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        context = browser_instance.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        url = f"http://127.0.0.1:{server.server_port}"
        page.goto(url)
        yield MutableConsole(page=page, repository=repository, url=url)
        context.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_initial_load_shows_search_form_and_hidden_pagination(browser_page: Page) -> None:
    expect(browser_page.locator("#job-id")).to_be_visible()
    expect(browser_page.locator("#posting-filter")).to_be_visible()
    expect(browser_page.locator("#theme-toggle")).to_be_visible()
    expect(browser_page.locator("#pagination")).to_be_hidden()


def test_search_returns_cards_with_verdict_dots(browser_page: Page) -> None:
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    cards = browser_page.locator(".result-card")
    assert cards.count() == 50

    first_card = cards.first
    expect(first_card.locator(".platform-tag")).to_be_visible()
    expect(first_card.locator(".card-company")).to_be_visible()
    expect(first_card.locator(".card-position")).to_be_visible()
    assert first_card.locator(".badge").count() >= 1
    assert first_card.get_attribute("data-verdict") is not None


def test_pagination_visible_for_large_result_set(browser_page: Page) -> None:
    expect(browser_page.locator("#pagination")).to_be_visible()
    page_info = browser_page.locator("#page-info")
    expect(page_info).to_contain_text("1 /")

    browser_page.locator("#page-next").click()
    expect(page_info).to_contain_text("2 /")
    cards_page2 = browser_page.locator(".result-card")
    assert cards_page2.count() > 0

    browser_page.locator("#page-prev").click()
    expect(page_info).to_contain_text("1 /")


def test_card_click_shows_detail_with_jd_and_screening(browser_page: Page) -> None:
    browser_page.locator(".result-card").first.click()
    jd = browser_page.locator("#jd-content")
    expect(jd).not_to_have_text("")
    expect(jd).to_contain_text("JD for position")
    expect(browser_page.locator("#detail-meta")).to_be_visible()


def test_screening_absent_shows_message(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("1003")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    screening = browser_page.locator("#screening-content")
    expect(screening).not_to_have_text("")
    expect(screening).to_contain_text("스크리닝 결과가 아직 없습니다")


def test_set_aside_record_is_named_apart_from_an_unscreened_one(browser_page: Page) -> None:
    # Both carry a null verdict. Collapsing them into 미생성 erases the reason the
    # pre-screen recorded, which is the distinction this state exists to draw.
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("8888")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    expect(browser_page.locator(".result-card").first).to_contain_text("사전 필터 제외")

    browser_page.locator(".result-card").first.click()
    expect(browser_page.locator("#detail-meta")).to_contain_text("사전 필터 제외")
    expect(browser_page.locator("#detail-meta")).to_contain_text("제목 제외 키워드 매칭")


def test_record_with_no_verdict_and_no_reason_still_reads_unscreened(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("1003")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    expect(browser_page.locator(".result-card").first).to_contain_text("미생성")


def test_empty_state_id_mismatch(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("99999")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".empty-state")
    expect(browser_page.locator(".empty-state")).to_contain_text("공고 ID 99999에 해당하는 레코드가 없습니다")
    expect(browser_page.locator("#pagination")).to_be_hidden()


def test_empty_state_filter_no_match(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#application-filter").select_option("offer")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".empty-state")
    expect(browser_page.locator(".empty-state")).to_contain_text("조건에 맞는 레코드가 없습니다")
    browser_page.locator("#application-filter").select_option("")


def test_posting_status_filter(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#posting-filter").select_option("closed")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    cards = browser_page.locator(".result-card")
    count = cards.count()
    assert count > 0
    for i in range(min(count, 5)):
        expect(cards.nth(i).locator(".posting-closed")).to_be_visible()
    browser_page.locator("#posting-filter").select_option("")


def test_theme_toggle_switches_mode(browser_page: Page) -> None:
    browser_page.locator("#theme-toggle").click()
    theme = browser_page.evaluate("document.documentElement.dataset.theme")
    assert theme in ("light", "dark")
    stored = browser_page.evaluate("localStorage.getItem('jd-console-theme')")
    assert stored == theme
    browser_page.locator("#theme-toggle").click()


def test_verdict_capped_detail(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("9999")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    meta = browser_page.locator("#detail-meta")
    expect(meta).to_contain_text("CappedCo")
    expect(meta).to_contain_text("보류 (대기)")


def test_xss_prevention_in_detail(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("1000")
    browser_page.locator("#search-form button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    jd = browser_page.locator("#jd-content")
    expect(jd).not_to_have_text("")
    assert browser_page.locator("#jd-content script").count() == 0


def test_console_save_appends_event_and_updates_current_status(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("42")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()

    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("1차 기술 면접")
    page.locator("#application-status-form button[type='submit']").click()

    expect(page.locator("#application-form-status")).to_contain_text("저장했습니다")
    expect(page.locator("#application-history li")).to_have_count(2)
    expect(page.locator("#detail-meta")).to_contain_text("면접")
    expect(page.locator("#application-history")).to_contain_text("1차 기술 면접")
    stored = mutable_console.repository.get(JobKey("wanted", "42"))
    assert stored.record.application_status is ApplicationStatus.INTERVIEW


def test_console_repeated_events_and_correction_stay_visible(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("42")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()

    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("1차 기술 면접")
    page.locator("#application-status-form button[type='submit']").click()
    expect(page.locator("#application-form-status")).to_contain_text("저장했습니다")
    page.locator("#application-note-input").fill("2차 기술 면접")
    page.locator("#application-status-form button[type='submit']").click()
    expect(page.locator("#application-form-status")).to_contain_text("저장했습니다")
    page.locator("#application-status-input").select_option("applied")
    page.locator("#application-note-input").fill("상태 수정")
    page.locator("#application-status-form button[type='submit']").click()
    expect(page.locator("#application-form-status")).to_contain_text("저장했습니다")

    history = page.locator("#application-history li")
    expect(history).to_have_count(4)
    expect(history.nth(1)).to_contain_text("1차 기술 면접")
    expect(history.nth(2)).to_contain_text("2차 기술 면접")
    expect(history.nth(3)).to_contain_text("상태 수정")


def test_console_renders_malicious_note_as_text(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("42")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()

    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("<img src=x onerror=alert(1)>")
    page.locator("#application-status-form button[type='submit']").click()

    expect(page.locator("#application-history")).to_contain_text("<img src=x onerror=alert(1)>")
    assert page.locator("#application-history img").count() == 0


def test_console_focuses_status_message_and_supports_375px_viewport(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.set_viewport_size({"width": 375, "height": 812})
    page.reload()
    page.locator("#job-id").fill("42")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()

    expect(page.locator("#application-status-form")).to_be_visible()
    expect(page.locator("#application-history")).to_be_visible()
    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("375px 확인")
    page.locator("#application-status-form button[type='submit']").click()
    expect(page.locator("#application-form-status")).to_be_focused()


def test_console_shows_synthesized_legacy_event(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("41")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()
    expect(page.locator("#application-history li")).to_have_count(1)
    expect(page.locator("#application-history")).to_contain_text("2026-07-31T09:00:00+09:00")
    expect(page.locator("#application-history")).to_contain_text("지원")


def test_console_ignores_stale_detail_response_and_clears_drafts(
    mutable_console: MutableConsole,
) -> None:
    page = mutable_console.page
    delayed_payload = json.dumps(
        {
            **mutable_console.repository.get(JobKey("wanted", "42")).record.to_dict(),
            "jd_markdown": "# delayed",
            "screening_markdown": None,
            "has_screening": False,
        },
        ensure_ascii=False,
    )

    page.route(
        "**/api/jobs/wanted/42",
        lambda route: (
            time.sleep(0.2),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=delayed_payload,
            ),
        )[-1],
    )
    page.locator("#job-id").fill("")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").nth(1).click()
    page.locator("#application-note-input").fill("임시 메모")
    page.locator("#application-occurred-at-input").fill("2026-08-10T09:30")

    page.unroute("**/api/jobs/wanted/42")
    page.locator(".result-card").nth(0).click()

    expect(page.locator("#detail-meta")).to_contain_text("Legacy")
    expect(page.locator("#application-note-input")).to_have_value("")
    expect(page.locator("#application-occurred-at-input")).to_have_value("")


def test_console_ignores_stale_patch_response_when_active_record_changes(
    mutable_console: MutableConsole,
) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").nth(1).click()
    expect(page.locator("#detail-meta")).to_contain_text("Example")
    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("늦게 도착한 응답")

    delayed_payload = json.dumps(
        {
            **mutable_console.repository.get(JobKey("wanted", "42")).record.to_dict(),
            "application_status": "interview",
            "application_status_updated_at": "2026-08-10T09:30:00+09:00",
            "application_history": [
                {
                    "status": "applied",
                    "occurred_at": "2026-07-30T09:00:00+09:00",
                    "note": None,
                },
                {
                    "status": "interview",
                    "occurred_at": "2026-08-10T09:30:00+09:00",
                    "note": "늦게 도착한 응답",
                },
            ],
            "jd_markdown": "# delayed",
            "screening_markdown": None,
            "has_screening": False,
            "index_refreshed": True,
            "index_warning": None,
        },
        ensure_ascii=False,
    )

    page.route(
        "**/api/jobs/wanted/42/application-status",
        lambda route: (
            time.sleep(0.2),
            route.fulfill(
                status=200,
                content_type="application/json",
                body=delayed_payload,
            ),
        )[-1],
    )
    page.locator("#application-status-form button[type='submit']").click()
    page.locator(".result-card").nth(0).click()
    expect(page.locator("#detail-meta")).to_contain_text("Legacy")
    expect(page.locator("#application-form-status")).to_have_text("")
    expect(page.locator("#application-history")).to_contain_text("2026-07-31T09:00:00+09:00")
    page.unroute("**/api/jobs/wanted/42/application-status")


def test_console_search_reflects_saved_status_after_refresh(mutable_console: MutableConsole) -> None:
    page = mutable_console.page
    page.locator("#job-id").fill("42")
    page.locator("#search-form button[type='submit']").click()
    page.wait_for_selector(".result-card")
    page.locator(".result-card").first.click()
    page.locator("#application-status-input").select_option("interview")
    page.locator("#application-note-input").fill("1차 기술 면접")
    page.locator("#application-status-form button[type='submit']").click()
    expect(page.locator("#application-form-status")).to_contain_text("저장했습니다")

    page.locator("#application-filter").select_option("interview")
    page.locator("#refresh-index").click()
    page.wait_for_selector(".result-card")
    expect(page.locator(".result-card")).to_have_count(1)
    expect(page.locator(".result-card").first).to_contain_text("면접")
