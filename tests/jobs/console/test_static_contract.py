from importlib import resources
import re

STATIC_PACKAGE = "careerkit.jobs.console.static"


def read(name: str) -> str:
    return resources.files(STATIC_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def test_console_markup_exposes_two_step_search_detail_and_accessible_status() -> None:
    html = read("index.html")
    assert 'id="job-id"' in html
    assert 'id="results"' in html
    assert 'id="detail-heading"' in html
    assert 'aria-live="polite"' in html
    assert 'id="back-to-results"' in html
    assert 'id="platform-filter"' in html
    assert 'id="verdict-filter"' in html
    assert 'id="application-filter"' in html
    assert 'id="refresh-index"' in html
    assert 'id="posting-filter"' in html
    assert 'name="posting_status"' in html
    assert 'id="application-status-form"' in html
    assert 'id="application-status-input"' in html
    assert 'id="application-note-input"' in html
    assert 'id="application-occurred-at-input"' in html
    assert 'id="application-history"' in html
    assert 'id="application-form-status"' in html


def test_console_renders_untrusted_markdown_as_text_and_handles_missing_screening() -> None:
    script = read("app.js")
    assert ".textContent" in script
    assert "innerHTML" not in script
    assert "스크리닝 결과가 아직 없습니다" in script
    assert "detailHeading.focus()" in script
    assert "resultsHeading.focus()" in script
    assert "applicationHistory" in script
    assert "applicationFormStatus" in script
    assert '#application-status-form' in script
    assert 'parameters.set("refresh", "1")' in script
    assert "dataset.verdict" in script


def test_console_has_responsive_layout_contract() -> None:
    styles = read("styles.css")
    assert "grid-template-columns" in styles
    assert "@media" in styles
    assert ".application-history" in styles
    assert ".application-status-form" in styles


def test_console_platform_filter_covers_all_canonical_source_types() -> None:
    html = read("index.html")
    for platform in (
        "wanted",
        "remember",
        "groupby",
        "saramin",
        "jobkorea",
        "jumpit",
        "offercent",
        "greeting",
        "private",
        "headhunter",
    ):
        assert f"<option>{platform}</option>" in html


def test_theme_bootstrap_script_exists_and_avoids_dom_mutation() -> None:
    script = read("theme.js")
    assert "localStorage" in script
    assert "innerHTML" not in script


def test_console_markup_exposes_theme_toggle() -> None:
    html = read("index.html")
    assert 'id="theme-toggle"' in html


def test_console_markup_exposes_pagination() -> None:
    html = read("index.html")
    assert 'id="pagination"' in html


def test_console_app_handles_verdict_capped() -> None:
    script = read("app.js")
    assert "verdict_capped" in script


def test_console_names_the_set_aside_state_separately_from_untouched() -> None:
    script = read("app.js")
    # A verdict-less record is either set aside with a recorded reason or never
    # processed; the badge must not collapse both into 미생성.
    assert "prescreen_reason" in script
    assert "사전 필터 제외" in script
    assert "미생성" in script


def test_console_scripts_are_external_only() -> None:
    html = read("index.html")
    for match in re.finditer(r"<script\b[^>]*>", html):
        assert "src=" in match.group(0)


def test_console_markup_has_no_inline_event_handlers() -> None:
    html = read("index.html")
    assert re.search(r'\son[a-z]+\s*=', html) is None
