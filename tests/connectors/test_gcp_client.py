"""GcpClient: bearer auth, host check, retries on 429/5xx, immediate 4xx errors."""

import httpx
import pytest

from ai_cost_tracker.connectors.gcp.client import GcpClient, retry_delay

URL = "https://monitoring.googleapis.com/v3/projects/p/timeSeries"


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0
        self.invalidated = 0
        self.closed = False

    async def token(self) -> str:
        self.calls += 1
        return f"ya29.token-{self.invalidated}"

    def invalidate(self) -> None:
        self.invalidated += 1

    async def close(self) -> None:
        self.closed = True


def make_client(handler, sleeps: list[float] | None = None, tokens=None, **kwargs) -> GcpClient:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GcpClient(tokens or FakeTokens(), http=http, sleep=sleep, **kwargs)


def sequence(*responses: httpx.Response):
    it = iter(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(it)

    return handler, seen


async def test_get_sends_bearer_and_params():
    handler, seen = sequence(httpx.Response(200, json={"timeSeries": []}))
    client = make_client(handler)
    assert await client.get(URL, {"filter": 'metric.type = "x"'}) == {"timeSeries": []}
    assert seen[0].headers["Authorization"] == "Bearer ya29.token-0"
    assert seen[0].url.params["filter"] == 'metric.type = "x"'


async def test_post_sends_json_and_params_to_regional_host():
    url = "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/x"
    handler, seen = sequence(httpx.Response(200, json={"ok": 1}))
    assert await make_client(handler).post(url, json={"a": 1}, params={"b": "2"}) == {"ok": 1}
    assert seen[0].method == "POST" and seen[0].content == b'{"a":1}'
    assert str(seen[0].url) == url + "?b=2"


async def test_empty_body_returns_empty_dict():
    handler, _ = sequence(httpx.Response(204))
    assert await make_client(handler).get(URL) == {}


@pytest.mark.parametrize(
    "url",
    ["/v3/projects/p", "https://evil.example/steal", "http://monitoring.googleapis.com/v3/x"],
)
async def test_refuses_relative_or_non_google_urls(url):
    client = make_client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        await client.get(url)


async def test_429_honours_retry_after_then_succeeds():
    handler, seen = sequence(
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"ok": True}),
    )
    sleeps: list[float] = []
    assert await make_client(handler, sleeps).get(URL) == {"ok": True}
    assert sleeps == [7.0] and len(seen) == 2


async def test_retry_after_is_capped():
    resp = httpx.Response(503, headers={"Retry-After": "3600"})
    assert retry_delay(resp, 0) == 60.0


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors_back_off_exponentially_then_raise(status):
    handler, seen = sequence(*[httpx.Response(status) for _ in range(3)])
    sleeps: list[float] = []
    client = make_client(handler, sleeps, max_retries=3)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get(URL)
    assert sleeps == [1.0, 2.0] and len(seen) == 3


async def test_other_4xx_raises_immediately_with_google_message():
    body = {
        "error": {
            "code": 403,
            "message": "Permission denied on project",
            "status": "PERMISSION_DENIED",
        }
    }
    handler, seen = sequence(httpx.Response(403, json=body))
    sleeps: list[float] = []
    with pytest.raises(httpx.HTTPStatusError) as err:
        await make_client(handler, sleeps).get(URL + "?pageToken=abc")
    assert err.value.response.status_code == 403
    assert "PERMISSION_DENIED Permission denied on project" in str(err.value)
    assert "pageToken" not in str(err.value) and "ya29" not in str(err.value)
    assert sleeps == [] and len(seen) == 1


async def test_401_refreshes_token_once():
    handler, seen = sequence(
        httpx.Response(401, json={"error": {"message": "expired"}}),
        httpx.Response(200, json={"ok": 1}),
    )
    tokens = FakeTokens()
    assert await make_client(handler, tokens=tokens).get(URL) == {"ok": 1}
    assert tokens.invalidated == 1
    assert seen[1].headers["Authorization"] == "Bearer ya29.token-1"


async def test_repeated_401_raises():
    handler, seen = sequence(httpx.Response(401), httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await make_client(handler).get(URL)
    assert len(seen) == 2


async def test_transport_errors_are_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("reset")
        return httpx.Response(200, json={"ok": 1})

    sleeps: list[float] = []
    assert await make_client(handler, sleeps).get(URL) == {"ok": 1}
    assert sleeps == [1.0]


async def test_aclose_closes_tokens():
    tokens = FakeTokens()
    client = make_client(lambda r: httpx.Response(200), tokens=tokens)
    await client.aclose()
    assert tokens.closed
