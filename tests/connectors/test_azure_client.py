"""ArmClient: auth header, retries on throttling, absolute nextLink URLs."""

import httpx
import pytest

from ai_cost_tracker.connectors.azure.client import ARM, ArmClient, retry_delay


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def token(self) -> str:
        self.calls += 1
        return "fake-token"


def make_client(handler, sleeps: list[float] | None = None, **kwargs) -> ArmClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(base_url=ARM, transport=httpx.MockTransport(handler))
    return ArmClient(FakeTokens(), http=http, sleep=sleep, **kwargs)


async def test_sends_bearer_token_and_relative_url():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    assert await client.get("/subscriptions?api-version=2022-12-01") == {"ok": True}
    assert seen[0].headers["Authorization"] == "Bearer fake-token"
    assert str(seen[0].url) == f"{ARM}/subscriptions?api-version=2022-12-01"


async def test_absolute_next_link_goes_to_that_url():
    seen: list[str] = []
    link = f"{ARM}/subscriptions/x/providers/Microsoft.CostManagement/query?$skiptoken=abc%3D"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    await make_client(handler).post(link, json={"q": 1})
    assert seen == [link]


async def test_refuses_to_send_token_to_other_hosts():
    client = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="evil.example"):
        await client.get("https://evil.example/steal")


async def test_429_then_success_respects_retry_after():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(
                429,
                headers={"x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "12"},
            ),
            httpx.Response(200, json={"done": 1}),
        ]
    )
    sleeps: list[float] = []
    client = make_client(lambda r: next(responses), sleeps)
    assert await client.post("/q", json={}) == {"done": 1}
    assert sleeps == [7.0, 12.0]


async def test_error_after_retries_raises():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"code": "ServiceUnavailable"}})

    sleeps: list[float] = []
    client = make_client(handler, sleeps, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get("/x")
    assert calls == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff without headers


async def test_client_errors_are_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})

    with pytest.raises(httpx.HTTPStatusError):
        await make_client(handler).get("/x")
    assert calls == 1


async def test_transport_errors_are_retried():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": 1})

    assert await make_client(handler).get("/x") == {"ok": 1}


def test_retry_delay_takes_longest_header_and_caps():
    resp = httpx.Response(
        429,
        headers={
            "Retry-After": "3",
            "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "9.5",
        },
    )
    assert retry_delay(resp, 0) == 9.5
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "9999"}), 0) == 120.0
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "soon"}), 2) == 4.0
