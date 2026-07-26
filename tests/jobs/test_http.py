from __future__ import annotations

import io
import urllib.error
from email.message import Message

import pytest

from careerkit.jobs.adapters.http import HttpError, HttpStatusError, UrllibHttpClient


def _http_error(url: str, code: int, headers: Message | None = None, body: bytes = b"") -> urllib.error.HTTPError:
    msg = headers if headers is not None else Message()
    return urllib.error.HTTPError(url, code, "status", msg, io.BytesIO(body))


def test_http_status_error_carries_status_and_url(monkeypatch) -> None:
    url = "https://example.com/jobs/123"

    def raise_404(*args, **kwargs):
        raise _http_error(url, 404, body=b"not found")

    monkeypatch.setattr("urllib.request.urlopen", raise_404)

    client = UrllibHttpClient()
    with pytest.raises(HttpStatusError) as exc_info:
        client.request_text(url)

    assert exc_info.value.status == 404
    assert url in str(exc_info.value)
    assert isinstance(exc_info.value, HttpError)
    assert exc_info.value.body == "not found"


def test_http_status_error_captures_json_body(monkeypatch) -> None:
    url = "https://jumpit-api.saramin.co.kr/api/position/99999999"
    body = b'{"message":" Entity Not Found","status":400,"errors":[],"code":"C003"}'

    def raise_400(*args, **kwargs):
        raise _http_error(url, 400, body=body)

    monkeypatch.setattr("urllib.request.urlopen", raise_400)

    client = UrllibHttpClient()
    with pytest.raises(HttpStatusError) as exc_info:
        client.request_text(url)

    assert exc_info.value.status == 400
    assert exc_info.value.body == body.decode("utf-8")


def test_http_status_error_bounds_body_read(monkeypatch) -> None:
    url = "https://example.com/jobs/large-error"

    class TrackingBody(io.BytesIO):
        read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            return super().read(size)

    body = TrackingBody(b"x" * 4096)

    def raise_500(*args, **kwargs):
        raise urllib.error.HTTPError(url, 500, "status", Message(), body)

    monkeypatch.setattr("urllib.request.urlopen", raise_500)

    with pytest.raises(HttpStatusError) as exc_info:
        UrllibHttpClient().request_text(url)

    assert body.read_sizes == [2048]
    assert exc_info.value.body == "x" * 2048


def test_custom_error_cls_is_unchanged(monkeypatch) -> None:
    class CustomError(RuntimeError):
        pass

    url = "https://example.com/jobs/456"

    def raise_404(*args, **kwargs):
        raise _http_error(url, 404)

    monkeypatch.setattr("urllib.request.urlopen", raise_404)

    client = UrllibHttpClient()
    with pytest.raises(CustomError) as exc_info:
        client.request_text(url, error_cls=CustomError)

    assert not isinstance(exc_info.value, HttpStatusError)
    assert not hasattr(exc_info.value, "status")


def test_retry_after_header_is_captured(monkeypatch) -> None:
    url = "https://example.com/jobs/789"
    headers = Message()
    headers["Retry-After"] = "3"

    def raise_429(*args, **kwargs):
        raise _http_error(url, 429, headers)

    monkeypatch.setattr("urllib.request.urlopen", raise_429)

    client = UrllibHttpClient()
    with pytest.raises(HttpStatusError) as exc_info:
        client.request_text(url)

    assert exc_info.value.status == 429
    assert exc_info.value.retry_after == "3"


def test_url_error_raises_plain_http_error_without_status(monkeypatch) -> None:
    url = "https://example.com/jobs/999"

    def raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

    client = UrllibHttpClient()
    with pytest.raises(HttpError) as exc_info:
        client.request_text(url)

    assert not isinstance(exc_info.value, HttpStatusError)
    assert not hasattr(exc_info.value, "status")
