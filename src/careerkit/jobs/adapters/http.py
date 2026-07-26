from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class HttpError(RuntimeError):
    pass


class HttpStatusError(HttpError):
    def __init__(self, message: str, *, status: int, retry_after: str | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.body = body


class HttpClient(Protocol):
    def request_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15, method: str = "GET", body: bytes | None = None, error_cls: type[Exception] = HttpError) -> dict: ...
    def request_text(self, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15, method: str = "GET", body: bytes | None = None, max_bytes: int | None = None, error_cls: type[Exception] = HttpError) -> str: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, _fp, code, msg, headers, _newurl):
        return None


@dataclass(frozen=True)
class UrllibHttpClient:
    def _headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        merged = {"User-Agent": DEFAULT_USER_AGENT}
        if headers:
            merged.update(headers)
        return merged

    def request_text(self, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15, method: str = "GET", body: bytes | None = None, max_bytes: int | None = None, error_cls: type[Exception] = HttpError) -> str:
        return self._request_text(
            url,
            headers=headers,
            timeout=timeout,
            method=method,
            body=body,
            max_bytes=max_bytes,
            error_cls=error_cls,
            open_url=urllib.request.urlopen,
        )

    def request_text_no_redirect(self, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15, method: str = "GET", body: bytes | None = None, max_bytes: int | None = None, error_cls: type[Exception] = HttpError) -> str:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        return self._request_text(
            url,
            headers=headers,
            timeout=timeout,
            method=method,
            body=body,
            max_bytes=max_bytes,
            error_cls=error_cls,
            open_url=opener.open,
        )

    def _request_text(self, url: str, *, headers: dict[str, str] | None, timeout: int, method: str, body: bytes | None, max_bytes: int | None, error_cls: type[Exception], open_url: Callable[..., Any]) -> str:
        req = urllib.request.Request(url, data=body, headers=self._headers(headers), method=method)
        try:
            with open_url(req, timeout=timeout) as response:
                payload = response.read(max_bytes) if max_bytes is not None else response.read()
                if max_bytes is not None:
                    payload = payload[:max_bytes]
                return payload.decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            if error_cls is HttpError:
                err_body: str | None
                try:
                    err_body = (exc.read(2048) or b"").decode("utf-8", errors="ignore")
                except OSError:
                    err_body = None
                raise HttpStatusError(f"HTTP {exc.code} for {url}", status=exc.code, retry_after=exc.headers.get("Retry-After"), body=err_body) from exc
            raise error_cls(f"HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise error_cls(f"URL error for {url}: {exc.reason}") from exc

    def request_json(self, url: str, *, headers: dict[str, str] | None = None, timeout: int = 15, method: str = "GET", body: bytes | None = None, error_cls: type[Exception] = HttpError) -> dict:
        text = self.request_text(url, headers=headers, timeout=timeout, method=method, body=body, error_cls=error_cls)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise error_cls(f"Invalid JSON response for {url}") from exc
