from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Callable, cast
from urllib.parse import urlparse

from careerkit.jobs.adapters.http import HttpClient, HttpError, HttpStatusError

_KST = timezone(timedelta(hours=9))
_RETRYABLE_STATUSES = (429, 503)
_MAX_RETRY_WAIT = 30.0
_DEFAULT_RETRY_WAIT = 5.0

_OFFERCENT_GENERIC_TITLE = "오퍼센트 | 성장하는 기업들의 채용 소식"
_SARAMIN_NOT_EXIST_MARKER = "not-exist-view"
_SARAMIN_POSTING_TITLE_MARKER = "모집 - 사람인"


class ProbeOutcome(Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


SUPPORTED_PLATFORMS = frozenset(
    {"wanted", "remember", "jumpit", "groupby", "offercent", "greeting", "saramin"}
)

RECHECK_SAFE_PLATFORMS = frozenset({"wanted", "remember", "jumpit", "greeting"})


class _RetryExhausted(Exception):
    pass


def _retry_wait(retry_after: str | None) -> float:
    if not retry_after:
        return _DEFAULT_RETRY_WAIT
    try:
        seconds = float(retry_after)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return _DEFAULT_RETRY_WAIT
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(seconds, _MAX_RETRY_WAIT))


def _call(request_fn, sleep):
    try:
        return request_fn()
    except HttpStatusError as exc:
        if exc.status not in _RETRYABLE_STATUSES:
            raise
        sleep(_retry_wait(exc.retry_after))
        try:
            return request_fn()
        except HttpStatusError as exc2:
            if exc2.status in _RETRYABLE_STATUSES:
                raise _RetryExhausted from exc2
            raise


def _probe_wanted(job_id, http, source_url, sleep):
    del source_url
    url = f"https://www.wanted.co.kr/api/chaos/jobs/v4/{job_id}/details"
    try:
        data = _call(lambda: http.request_json(url, headers={"Accept": "application/json"}), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpStatusError as exc:
        return ProbeOutcome.CLOSED if exc.status == 404 else ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    if not isinstance(data, Mapping):
        return ProbeOutcome.UNKNOWN
    envelope = data.get("data")
    if not isinstance(envelope, Mapping):
        return ProbeOutcome.UNKNOWN
    job = envelope.get("job")
    if not isinstance(job, Mapping):
        return ProbeOutcome.UNKNOWN
    status = job.get("status")
    if status in ("close", "draft"):
        return ProbeOutcome.CLOSED
    if status == "active":
        return ProbeOutcome.ACTIVE
    return ProbeOutcome.UNKNOWN


def _probe_remember(job_id, http, source_url, sleep):
    del source_url
    url = f"https://career-api.rememberapp.co.kr/job_postings/{job_id}"
    try:
        data = _call(lambda: http.request_json(url, headers={"Accept": "application/json"}), sleep)
    except (_RetryExhausted, HttpStatusError, HttpError):
        return ProbeOutcome.UNKNOWN
    if not isinstance(data, Mapping):
        return ProbeOutcome.UNKNOWN
    envelope = data.get("data")
    if not isinstance(envelope, Mapping):
        return ProbeOutcome.UNKNOWN
    status = envelope.get("status")
    if status == "closed":
        return ProbeOutcome.CLOSED
    if status == "published":
        return ProbeOutcome.ACTIVE
    return ProbeOutcome.UNKNOWN


def _probe_jumpit(job_id, http, source_url, sleep):
    del source_url
    url = f"https://jumpit-api.saramin.co.kr/api/position/{job_id}"
    try:
        data = _call(lambda: http.request_json(url, headers={"Accept": "application/json"}), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpStatusError as exc:
        return ProbeOutcome.CLOSED if exc.status == 400 and exc.body and "C003" in exc.body else ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    if not isinstance(data, Mapping):
        return ProbeOutcome.UNKNOWN
    result = data.get("result")
    if not isinstance(result, dict) or "alwaysOpen" not in result:
        return ProbeOutcome.UNKNOWN
    if result["alwaysOpen"]:
        return ProbeOutcome.ACTIVE
    closed_at = result.get("closedAt")
    if not closed_at:
        return ProbeOutcome.UNKNOWN
    try:
        closed_dt = datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_KST)
    except (TypeError, ValueError):
        return ProbeOutcome.UNKNOWN
    return ProbeOutcome.CLOSED if closed_dt < datetime.now(_KST) else ProbeOutcome.ACTIVE


def _probe_groupby(job_id, http, source_url, sleep):
    del source_url
    url = f"https://api.groupby.kr/startup-positions/{job_id}"
    try:
        _call(lambda: http.request_json(url, headers={"Accept": "application/json"}), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpStatusError as exc:
        if exc.status == 404 and exc.body and "비공개된 채용 공고" in _unescape_unicode(exc.body):
            return ProbeOutcome.CLOSED
        return ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    return ProbeOutcome.ACTIVE


def _unescape_unicode(text: str) -> str:
    try:
        return text.encode("latin-1", errors="backslashreplace").decode("unicode_escape")
    except UnicodeDecodeError:
        return text


def _probe_offercent(job_id, http, source_url, sleep):
    del source_url
    url = f"https://offercent.co.kr/jd/{job_id}"
    try:
        text = _call(lambda: http.request_text(url), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    match = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
    if not match:
        return ProbeOutcome.UNKNOWN
    title = match.group(1).strip()
    return ProbeOutcome.CLOSED if title == _OFFERCENT_GENERIC_TITLE else ProbeOutcome.ACTIVE


def _probe_greeting(job_id, http, source_url, sleep):
    if not source_url or not _valid_greeting_url(source_url, job_id):
        return ProbeOutcome.UNKNOWN
    request_no_redirect = getattr(http, "request_text_no_redirect", None)
    if not callable(request_no_redirect):
        return ProbeOutcome.UNKNOWN
    request_no_redirect = cast(Callable[[str], str], request_no_redirect)
    try:
        text = _call(lambda: request_no_redirect(source_url), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpStatusError as exc:
        return ProbeOutcome.CLOSED if exc.status == 404 else ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    anchor = text.find('"openingsInfo"')
    if anchor == -1:
        return ProbeOutcome.UNKNOWN
    match = re.search(r'"status":"(\w+)"', text[anchor : anchor + 300])
    if not match:
        return ProbeOutcome.UNKNOWN
    status = match.group(1)
    if status == "OPEN":
        return ProbeOutcome.ACTIVE
    if status == "CLOSED":
        return ProbeOutcome.CLOSED
    return ProbeOutcome.UNKNOWN


def _valid_greeting_url(source_url: str, job_id: str) -> bool:
    parsed = urlparse(source_url)
    hostname = parsed.hostname or ""
    expected_path = rf"/(?:[a-z]{{2}}/)?o/{re.escape(job_id)}/?"
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and hostname.endswith(".career.greetinghr.com")
        and port is None
        and parsed.username is None
        and parsed.password is None
        and re.fullmatch(expected_path, parsed.path) is not None
    )


def _probe_saramin(job_id, http, source_url, sleep):
    del source_url
    url = f"https://www.saramin.co.kr/zf_user/jobs/relay/view?rec_idx={job_id}"
    try:
        text = _call(lambda: http.request_text(url), sleep)
    except _RetryExhausted:
        return ProbeOutcome.UNKNOWN
    except HttpError:
        return ProbeOutcome.UNKNOWN
    if _SARAMIN_NOT_EXIST_MARKER in text:
        return ProbeOutcome.CLOSED
    match = re.search(r"마감일:(\d{4}-\d{2}-\d{2})", text)
    if match:
        try:
            deadline = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return ProbeOutcome.UNKNOWN
        return ProbeOutcome.CLOSED if deadline < datetime.now(_KST).date() else ProbeOutcome.ACTIVE
    if _SARAMIN_POSTING_TITLE_MARKER in text:
        return ProbeOutcome.ACTIVE
    return ProbeOutcome.UNKNOWN


_PROBES = {
    "wanted": _probe_wanted,
    "remember": _probe_remember,
    "jumpit": _probe_jumpit,
    "groupby": _probe_groupby,
    "offercent": _probe_offercent,
    "greeting": _probe_greeting,
    "saramin": _probe_saramin,
}


def probe_posting_status(
    platform: str,
    job_id: str,
    http: HttpClient,
    *,
    source_url: str | None = None,
    sleep=time.sleep,
) -> ProbeOutcome:
    if platform not in SUPPORTED_PLATFORMS:
        return ProbeOutcome.UNKNOWN
    return _PROBES[platform](job_id, http, source_url, sleep)
