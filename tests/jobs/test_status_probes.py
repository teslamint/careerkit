from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from careerkit.jobs.adapters.http import HttpError, HttpStatusError
from careerkit.jobs.adapters.status_probes import (
    SUPPORTED_PLATFORMS,
    ProbeOutcome,
    probe_posting_status,
)


class FakeHttpClient:
    def __init__(self, json_queue=None, text_queue=None) -> None:
        self.json_queue = list(json_queue or [])
        self.text_queue = list(text_queue or [])
        self.requests: list[tuple[str, str]] = []

    def request_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
        error_cls: type[Exception] = HttpError,
    ) -> dict:
        self.requests.append(("json", url))
        item = self.json_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 15,
        method: str = "GET",
        body: bytes | None = None,
        max_bytes: int | None = None,
        error_cls: type[Exception] = HttpError,
    ) -> str:
        self.requests.append(("text", url))
        item = self.text_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_text_no_redirect(self, url: str, **kwargs) -> str:
        return self.request_text(url, **kwargs)


def _no_sleep(_seconds):
    pass


# --- JSON platforms: happy path ---


def test_wanted_active():
    http = FakeHttpClient(json_queue=[{"data": {"job": {"status": "active"}}}])
    assert probe_posting_status("wanted", "100007", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_wanted_closed():
    http = FakeHttpClient(json_queue=[{"data": {"job": {"status": "close"}}}])
    assert probe_posting_status("wanted", "50000", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_wanted_draft_is_closed():
    http = FakeHttpClient(json_queue=[{"data": {"job": {"status": "draft"}}}])
    assert probe_posting_status("wanted", "100008", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_remember_active():
    http = FakeHttpClient(json_queue=[{"data": {"status": "published"}}])
    assert probe_posting_status("remember", "100009", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_remember_closed():
    http = FakeHttpClient(json_queue=[{"data": {"status": "closed"}}])
    assert probe_posting_status("remember", "100010", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_wanted_malformed_envelope_is_unknown():
    for payload in ({"data": None}, {"data": []}, {"data": {"job": None}}):
        http = FakeHttpClient(json_queue=[payload])
        assert probe_posting_status("wanted", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_remember_malformed_envelope_is_unknown():
    for payload in ({"data": None}, {"data": []}):
        http = FakeHttpClient(json_queue=[payload])
        assert probe_posting_status("remember", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_json_probes_reject_non_object_top_level_payloads():
    for platform in ("wanted", "remember", "jumpit"):
        for payload in (None, [], "error"):
            http = FakeHttpClient(json_queue=[payload])
            assert probe_posting_status(platform, "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_jumpit_active():
    http = FakeHttpClient(
        json_queue=[{"code": "C001", "result": {"alwaysOpen": False, "closedAt": "2026-08-17 23:59:59"}}]
    )
    assert probe_posting_status("jumpit", "54487088", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_jumpit_closed():
    http = FakeHttpClient(
        json_queue=[{"code": "C001", "result": {"alwaysOpen": False, "closedAt": "2026-02-04 23:59:59"}}]
    )
    assert probe_posting_status("jumpit", "52739569", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_jumpit_non_string_deadline_is_unknown():
    http = FakeHttpClient(
        json_queue=[{"code": "C001", "result": {"alwaysOpen": False, "closedAt": 20260817}}]
    )
    assert probe_posting_status("jumpit", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_groupby_active():
    http = FakeHttpClient(json_queue=[{"status": 200, "data": {}}])
    assert probe_posting_status("groupby", "100013", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_groupby_closed_via_404_with_unpublished_body():
    http = FakeHttpClient(json_queue=[HttpStatusError("not found", status=404, body='{"status": 404, "msg": "비공개된 채용 공고입니다."}')])
    assert probe_posting_status("groupby", "999999", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_groupby_closed_via_404_with_unicode_escaped_body():
    body = '{"status": 404, "data": null, "msg": "\\ube44\\uacf5\\uac1c\\ub41c \\ucc44\\uc6a9 \\uacf5\\uace0\\uc785\\ub2c8\\ub2e4."}'
    http = FakeHttpClient(json_queue=[HttpStatusError("not found", status=404, body=body)])
    assert probe_posting_status("groupby", "999999", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_groupby_bare_404_is_unknown():
    http = FakeHttpClient(json_queue=[HttpStatusError("not found", status=404)])
    assert probe_posting_status("groupby", "999999", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


# --- HTML platforms: happy path ---


def test_offercent_active():
    html = "<title>[company] position | 오퍼센트</title>"
    http = FakeHttpClient(text_queue=[html])
    assert probe_posting_status("offercent", "211086", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_offercent_closed():
    html = "<title>오퍼센트 | 성장하는 기업들의 채용 소식</title>"
    http = FakeHttpClient(text_queue=[html])
    assert probe_posting_status("offercent", "99999999", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_greeting_active():
    html = '{"queries":[{"state":{"status":"success"}}],"openingsInfo":{"openingId":100012,"status":"OPEN"}}'
    http = FakeHttpClient(text_queue=[html])
    outcome = probe_posting_status(
        "greeting", "100012", http, source_url="https://acme.career.greetinghr.com/ko/o/100012", sleep=_no_sleep
    )
    assert outcome is ProbeOutcome.ACTIVE


def test_greeting_ignores_react_query_status_noise():
    html = '{"queries":[{"state":{"status":"success"}}]}'
    http = FakeHttpClient(text_queue=[html])
    outcome = probe_posting_status(
        "greeting", "100012", http, source_url="https://acme.career.greetinghr.com/ko/o/100012", sleep=_no_sleep
    )
    assert outcome is ProbeOutcome.UNKNOWN


def test_greeting_closed_status_is_closed():
    html = '"openingsInfo":{"openingId":100012,"status":"CLOSED"}'
    http = FakeHttpClient(text_queue=[html])
    outcome = probe_posting_status(
        "greeting", "100012", http, source_url="https://acme.career.greetinghr.com/ko/o/100012", sleep=_no_sleep
    )
    assert outcome is ProbeOutcome.CLOSED


def test_greeting_unverified_status_is_unknown():
    html = '"openingsInfo":{"openingId":100012,"status":"DRAFT"}'
    http = FakeHttpClient(text_queue=[html])
    outcome = probe_posting_status(
        "greeting", "100012", http, source_url="https://acme.career.greetinghr.com/ko/o/100012", sleep=_no_sleep
    )
    assert outcome is ProbeOutcome.UNKNOWN


def test_greeting_closed_via_404():
    http = FakeHttpClient(text_queue=[HttpStatusError("not found", status=404)])
    outcome = probe_posting_status(
        "greeting", "99999999", http, source_url="https://acme.career.greetinghr.com/ko/o/99999999", sleep=_no_sleep
    )
    assert outcome is ProbeOutcome.CLOSED


def test_greeting_without_source_url_is_unknown():
    http = FakeHttpClient()
    outcome = probe_posting_status("greeting", "100012", http, sleep=_no_sleep)
    assert outcome is ProbeOutcome.UNKNOWN
    assert http.requests == []


def test_greeting_rejects_untrusted_or_mismatched_source_url():
    urls = (
        "http://acme.career.greetinghr.com/ko/o/100012",
        "https://career.greetinghr.com.evil.example/ko/o/100012",
        "https://acme.career.greetinghr.com/ko/o/999999",
        "https://127.0.0.1/ko/o/100012",
    )
    for source_url in urls:
        http = FakeHttpClient(text_queue=['"openingsInfo":{"status":"OPEN"}'])
        outcome = probe_posting_status(
            "greeting", "100012", http, source_url=source_url, sleep=_no_sleep
        )
        assert outcome is ProbeOutcome.UNKNOWN
        assert http.requests == []


def test_greeting_rejects_malformed_ports():
    for port in ("not-a-port", "99999"):
        http = FakeHttpClient(text_queue=['"openingsInfo":{"status":"OPEN"}'])
        outcome = probe_posting_status(
            "greeting",
            "100012",
            http,
            source_url=f"https://acme.career.greetinghr.com:{port}/ko/o/100012",
            sleep=_no_sleep,
        )
        assert outcome is ProbeOutcome.UNKNOWN
        assert http.requests == []


def test_saramin_active_future_deadline():
    html = "모집 - 사람인 마감일:2026-08-31"
    http = FakeHttpClient(text_queue=[html])
    assert probe_posting_status("saramin", "54352654", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_saramin_closed_past_deadline():
    html = "모집 - 사람인 마감일:2025-02-28"
    http = FakeHttpClient(text_queue=[html])
    assert probe_posting_status("saramin", "50000000", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_saramin_deadline_today_is_active():
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    http = FakeHttpClient(text_queue=[f"모집 - 사람인 마감일:{today}"])
    assert probe_posting_status("saramin", "54352654", http, sleep=_no_sleep) is ProbeOutcome.ACTIVE


def test_saramin_invalid_deadline_is_unknown():
    http = FakeHttpClient(text_queue=["모집 - 사람인 마감일:2026-99-99"])
    assert probe_posting_status("saramin", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_saramin_closed_not_exist_page():
    html = "<html>recruit/not-exist-view</html>"
    http = FakeHttpClient(text_queue=[html])
    assert probe_posting_status("saramin", "30000000", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


# --- edges ---


def test_unsupported_platform_is_unknown_with_no_http_call():
    http = FakeHttpClient()
    assert probe_posting_status("headhunter", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN
    assert http.requests == []
    assert "headhunter" not in SUPPORTED_PLATFORMS


def test_wanted_404_is_closed():
    http = FakeHttpClient(json_queue=[HttpStatusError("not found", status=404)])
    assert probe_posting_status("wanted", "200000", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_remember_404_is_unknown():
    http = FakeHttpClient(json_queue=[HttpStatusError("not found", status=404)])
    assert probe_posting_status("remember", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_jumpit_400_with_c003_is_closed():
    http = FakeHttpClient(json_queue=[HttpStatusError("bad request", status=400, body='{"code":"C003"}')])
    assert probe_posting_status("jumpit", "99999999", http, sleep=_no_sleep) is ProbeOutcome.CLOSED


def test_jumpit_400_without_c003_is_unknown():
    http = FakeHttpClient(json_queue=[HttpStatusError("bad request", status=400, body='{"code":"C999"}')])
    assert probe_posting_status("jumpit", "99999999", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_wanted_500_is_unknown():
    http = FakeHttpClient(json_queue=[HttpStatusError("server error", status=500)])
    assert probe_posting_status("wanted", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


# --- retry on 429/503 ---


def test_retry_after_429_then_success():
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=429, retry_after="1"),
            {"data": {"job": {"status": "active"}}},
        ]
    )
    slept = []
    outcome = probe_posting_status("wanted", "1", http, sleep=slept.append)
    assert outcome is ProbeOutcome.ACTIVE
    assert slept == [1.0]
    assert len(http.requests) == 2


def test_retry_after_http_date_then_success():
    retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=429, retry_after=retry_at),
            {"data": {"job": {"status": "active"}}},
        ]
    )
    slept = []
    assert probe_posting_status("wanted", "1", http, sleep=slept.append) is ProbeOutcome.ACTIVE
    assert len(slept) == 1
    assert 10.0 <= slept[0] <= 20.0


def test_negative_retry_after_is_clamped_to_zero():
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=429, retry_after="-1"),
            {"data": {"job": {"status": "active"}}},
        ]
    )
    slept = []
    outcome = probe_posting_status("wanted", "1", http, sleep=slept.append)
    assert outcome is ProbeOutcome.ACTIVE
    assert slept == [0.0]


def test_two_consecutive_429s_is_unknown():
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=429, retry_after="1"),
            HttpStatusError("throttled", status=429, retry_after="1"),
        ]
    )
    outcome = probe_posting_status("wanted", "1", http, sleep=_no_sleep)
    assert outcome is ProbeOutcome.UNKNOWN
    assert len(http.requests) == 2


def test_retry_sleep_is_capped_at_30():
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=503, retry_after="9000"),
            {"data": {"job": {"status": "active"}}},
        ]
    )
    slept = []
    probe_posting_status("wanted", "1", http, sleep=slept.append)
    assert slept == [30.0]


def test_retry_default_wait_without_retry_after():
    http = FakeHttpClient(
        json_queue=[
            HttpStatusError("throttled", status=503, retry_after=None),
            {"data": {"job": {"status": "active"}}},
        ]
    )
    slept = []
    probe_posting_status("wanted", "1", http, sleep=slept.append)
    assert slept == [5.0]


# --- errors ---


def test_plain_http_error_is_unknown():
    http = FakeHttpClient(json_queue=[HttpError("transport failed")])
    assert probe_posting_status("wanted", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN


def test_missing_indicator_field_is_unknown():
    http = FakeHttpClient(json_queue=[{"data": {}}])
    assert probe_posting_status("wanted", "1", http, sleep=_no_sleep) is ProbeOutcome.UNKNOWN
