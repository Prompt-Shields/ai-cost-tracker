"""Thin async client for Azure Resource Manager, authenticated as the app's managed identity.

In production the app runs with a managed identity that holds ``Reader`` on each tracked
subscription (ARCHITECTURE.md §4.3). Locally, ``DefaultAzureCredential`` falls back to the
developer's ``az login``.
"""

import asyncio
from typing import Any

import httpx

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"

# Cost Management throttles per scope; back off on 429 using Retry-After.
_MAX_RETRIES = 5


class TokenProvider:
    """Wraps ``azure.identity.aio.DefaultAzureCredential`` so tests can swap it out."""

    def __init__(self) -> None:
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()

    async def token(self) -> str:
        return (await self._credential.get_token(ARM_SCOPE)).token

    async def close(self) -> None:
        await self._credential.close()


class ArmClient:
    def __init__(self, tokens: Any, http: httpx.AsyncClient | None = None) -> None:
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(base_url=ARM, timeout=60)

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(_MAX_RETRIES):
            headers = {"Authorization": f"Bearer {await self._tokens.token()}"}
            resp = await self._http.request(method, url, headers=headers, **kwargs)
            if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_retry_after(resp, attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    async def get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, json: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", url, json=json)

    async def aclose(self) -> None:
        await self._http.aclose()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    for header in (
        "Retry-After",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    ):
        value = resp.headers.get(header)
        if value and value.isdigit():
            return float(value)
    return float(2**attempt)
