"""Thin async client for Azure Resource Manager, authenticated as the app's managed identity.

In production the app runs with a managed identity that holds ``Reader`` on each tracked
subscription (ARCHITECTURE.md §4.3). Locally, ``DefaultAzureCredential`` falls back to the
developer's ``az login``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

ARM = "https://management.azure.com"
ARM_HOST = "management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"

_MAX_RETRIES = 5
_MAX_SLEEP = 120.0
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Cost Management sends its own throttling headers instead of (or as well as) Retry-After.
# Header names are from Microsoft's Cost Management throttling docs; which ones a
# pay-as-you-go subscription actually returns is unverified until the phase 0 spike (#2).
_RETRY_HEADERS = (
    "Retry-After",
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-client-retry-after",
)


class Tokens(Protocol):
    async def token(self) -> str: ...


class TokenProvider:
    """Wraps ``azure.identity.DefaultAzureCredential`` so tests can swap it out.

    Uses the sync credential in a worker thread: the ``azure.identity.aio`` variant needs
    ``aiohttp``, which we don't otherwise depend on. The credential caches tokens and
    refreshes them before expiry, so calling ``token()`` per request is cheap.
    """

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential

        self._credential = DefaultAzureCredential()

    async def token(self) -> str:
        return (await asyncio.to_thread(self._credential.get_token, ARM_SCOPE)).token

    async def close(self) -> None:
        self._credential.close()


class ArmClient:
    """GET/POST against ARM with bearer auth and throttling-aware retries.

    URLs may be ARM-relative (``/subscriptions/...``) or absolute: Cost Management and ARM
    return ``nextLink`` as an absolute URL. Absolute URLs must point at ARM so the bearer
    token is never sent to another host.
    """

    def __init__(
        self,
        tokens: Tokens,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(base_url=ARM, timeout=60)
        self._sleep = sleep
        self._max_retries = max_retries

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        _check_host(url)
        for attempt in range(self._max_retries):
            last = attempt == self._max_retries - 1
            headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError:
                if last:
                    raise
                await self._sleep(_backoff(attempt))
                continue
            if resp.status_code in _RETRY_STATUSES and not last:
                await self._sleep(retry_delay(resp, attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    async def get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self.request("GET", url, params=params)

    async def post(self, url: str, json: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", url, json=json)

    async def aclose(self) -> None:
        await self._http.aclose()
        if close := getattr(self._tokens, "close", None):
            await close()


def retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Longest wait any throttling header asks for, else exponential backoff; capped."""
    waits = [w for h in _RETRY_HEADERS if (w := _parse_wait(resp.headers.get(h))) is not None]
    return min(max(waits), _MAX_SLEEP) if waits else _backoff(attempt)


def _parse_wait(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:  # Retry-After may also be an HTTP date.
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


def _backoff(attempt: int) -> float:
    return min(float(2**attempt), _MAX_SLEEP)


def _check_host(url: str) -> None:
    parsed = httpx.URL(url)
    if parsed.is_absolute_url and parsed.host != ARM_HOST:
        raise ValueError(f"refusing to send ARM token to {parsed.host}")
