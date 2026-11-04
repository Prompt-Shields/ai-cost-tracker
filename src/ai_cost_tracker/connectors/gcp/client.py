"""Async REST client for Google APIs, authenticated through WIF (``auth.py``).

Mirrors ``connectors/azure/client.py``: bearer auth, retries with backoff on throttling and
transient server errors, and a host check so the token only ever goes to Google APIs.
Callers pass absolute URLs because Google services live on many hosts (BigQuery, Cloud
Monitoring, regional Vertex AI endpoints, ...).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from ai_cost_tracker.connectors.gcp.auth import google_error

_MAX_RETRIES = 5
_MAX_SLEEP = 60.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_GOOGLE_SUFFIX = ".googleapis.com"


class Tokens(Protocol):
    async def token(self) -> str: ...


class GcpClient:
    """GET/POST JSON against Google REST APIs.

    429/5xx and transport errors are retried (``Retry-After`` honoured, capped); other 4xx
    raise ``httpx.HTTPStatusError`` at once with Google's error message, never a token.
    A 401 drops the cached token once, in case it was revoked before its expiry.
    """

    def __init__(
        self,
        tokens: Tokens,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(timeout=60)
        self._sleep = sleep
        self._max_retries = max_retries

    @property
    def tokens(self) -> Tokens:
        return self._tokens

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        _check_url(url)
        refreshed = False
        attempt = 0
        while True:
            last = attempt >= self._max_retries - 1
            headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError:
                if last:
                    raise
                await self._sleep(_backoff(attempt))
                attempt += 1
                continue
            if resp.status_code == 401 and not refreshed and hasattr(self._tokens, "invalidate"):
                self._tokens.invalidate()
                refreshed = True
                continue
            if resp.status_code in _RETRY_STATUSES and not last:
                await self._sleep(retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.is_error:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code} from {method} {_safe_url(url)}: {google_error(resp)}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json() if resp.content else {}

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", url, params=params)

    async def post(
        self, url: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("POST", url, json=json, params=params)

    async def aclose(self) -> None:
        await self._http.aclose()
        if close := getattr(self._tokens, "close", None):
            await close()


def retry_delay(resp: httpx.Response, attempt: int) -> float:
    """``Retry-After`` if Google sent one, else exponential backoff; capped."""
    wait = _parse_wait(resp.headers.get("Retry-After"))
    return min(wait if wait is not None else _backoff(attempt), _MAX_SLEEP)


def _parse_wait(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta seconds or an HTTP date)."""
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _backoff(attempt: int) -> float:
    return min(float(2**attempt), _MAX_SLEEP)


def _check_url(url: str) -> None:
    parsed = httpx.URL(url)
    if not parsed.is_absolute_url:
        raise ValueError(f"GcpClient needs an absolute URL, got {url!r}")
    host = parsed.host
    if parsed.scheme != "https" or not (host == "googleapis.com" or host.endswith(_GOOGLE_SUFFIX)):
        raise ValueError(f"refusing to send a Google token to {parsed.scheme}://{host}")


def _safe_url(url: str) -> str:
    """URL without its query string (page tokens and filters make errors unreadable)."""
    return url.split("?", 1)[0]


__all__ = ["GcpClient", "retry_delay"]
