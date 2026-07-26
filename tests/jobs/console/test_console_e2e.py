"""Playwright E2E tests for the JD Console frontend."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator

import pytest

pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
Page = pw.Page
expect = pw.expect
sync_playwright = pw.sync_playwright

from careerkit.jobs.adapters.storage.file_records import JDRecordRepository
from careerkit.jobs.console.server import create_server
from careerkit.jobs.domain.model import ApplicationStatus, JobKey, JobRecord, PostingStatus, ScreeningVerdict


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
    server = create_server(
        records_root=records,
        database_path=tmp_path / "derived" / "search.sqlite3",
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
def browser_page(server_url: str) -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(server_url)
        yield page
        browser.close()


def test_initial_load_shows_search_form_and_hidden_pagination(browser_page: Page) -> None:
    expect(browser_page.locator("#job-id")).to_be_visible()
    expect(browser_page.locator("#posting-filter")).to_be_visible()
    expect(browser_page.locator("#theme-toggle")).to_be_visible()
    expect(browser_page.locator("#pagination")).to_be_hidden()


def test_search_returns_cards_with_verdict_dots(browser_page: Page) -> None:
    browser_page.locator("button[type='submit']").click()
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
    browser_page.locator("button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    screening = browser_page.locator("#screening-content")
    expect(screening).not_to_have_text("")
    expect(screening).to_contain_text("스크리닝 결과가 아직 없습니다")


def test_empty_state_id_mismatch(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("99999")
    browser_page.locator("button[type='submit']").click()
    browser_page.wait_for_selector(".empty-state")
    expect(browser_page.locator(".empty-state")).to_contain_text("공고 ID 99999에 해당하는 레코드가 없습니다")
    expect(browser_page.locator("#pagination")).to_be_hidden()


def test_empty_state_filter_no_match(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#application-filter").select_option("offer")
    browser_page.locator("button[type='submit']").click()
    browser_page.wait_for_selector(".empty-state")
    expect(browser_page.locator(".empty-state")).to_contain_text("조건에 맞는 레코드가 없습니다")
    browser_page.locator("#application-filter").select_option("")


def test_posting_status_filter(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#posting-filter").select_option("closed")
    browser_page.locator("button[type='submit']").click()
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
    browser_page.locator("button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    meta = browser_page.locator("#detail-meta")
    expect(meta).to_contain_text("CappedCo")
    expect(meta).to_contain_text("보류 (대기)")


def test_xss_prevention_in_detail(browser_page: Page) -> None:
    browser_page.locator("#job-id").fill("")
    browser_page.locator("#job-id").fill("1000")
    browser_page.locator("button[type='submit']").click()
    browser_page.wait_for_selector(".result-card")
    browser_page.locator(".result-card").first.click()
    jd = browser_page.locator("#jd-content")
    expect(jd).not_to_have_text("")
    assert browser_page.locator("#jd-content script").count() == 0
