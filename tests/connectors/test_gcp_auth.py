"""WifTokenProvider: Azure token -> STS exchange -> SA impersonation, caching, errors."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from ai_cost_tracker.connectors.gcp.auth import (
    ACCESS_TOKEN_TYPE,
    CLOUD_PLATFORM,
    JWT_TOKEN_TYPE,
    STS_URL,
    TOKEN_EXCHANGE_GRANT,
    GcpAuthError,
    WifTokenProvider,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gcp" / "auth"
AUDIENCE = (
    "//iam.googleapis.com/projects/123456/locations/global"
    "/workloadIdentityPools/act/providers/entra"
)
SA = "act-reader@act-host.iam.gserviceaccount.com"
SCOPE = "api://AzureADTokenExchange/.default"
AZURE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtaSJ9.c2lnbmF0dXJl"
IMPERSONATE_URL = (
    f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{SA}:generateAccessToken"
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeAzureCredential:
    def __init__(self, error: Exception | None = None) -> None:
        self.scopes: list[str] = []
        self.error = error
        self.closed = False

    def get_token(self, scope: str):
        self.scopes.append(scope)
        if self.error:
            raise self.error
        return SimpleNamespace(token=AZURE_JWT, expires_on=0)

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def google(handler_overrides: dict | None = None, seen: list | None = None):
    overrides = handler_overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        if url in overrides:
            return overrides[url](request)
        if url == STS_URL:
            return httpx.Response(200, json=fixture("sts_token.json"))
        if url == IMPERSONATE_URL:
            return httpx.Response(200, json=fixture("generate_access_token.json"))
        return httpx.Response(404, json={"error": {"message": "unexpected " + url}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def provider(http, credential=None, clock=None, **kwargs) -> WifTokenProvider:
    args = {"audience": AUDIENCE, "service_account": SA, "azure_scope": SCOPE} | kwargs
    return WifTokenProvider(
        **args,
        azure_credential=credential or FakeAzureCredential(),
        http=http,
        clock=clock or Clock(),
    )


async def test_full_flow_sends_documented_request_bodies():
    seen: list[httpx.Request] = []
    cred = FakeAzureCredential()
    tokens = provider(google(seen=seen), cred)

    assert await tokens.token() == "ya29.fake-service-account-token"
    assert cred.scopes == [SCOPE]

    sts, iam = seen
    assert str(sts.url) == STS_URL and sts.method == "POST"
    assert sts.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(sts.content.decode()).items()}
    assert form == {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "audience": AUDIENCE,
        "scope": CLOUD_PLATFORM,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "subject_token": AZURE_JWT,
        "subject_token_type": JWT_TOKEN_TYPE,
    }
    assert "authorization" not in sts.headers

    assert str(iam.url) == IMPERSONATE_URL
    assert iam.headers["authorization"] == "Bearer ya29.fake-federated-token"
    assert json.loads(iam.content) == {"scope": [CLOUD_PLATFORM], "lifetime": "3600s"}


async def test_audience_without_prefix_is_normalised():
    seen: list[httpx.Request] = []
    tokens = provider(google(seen=seen), audience=AUDIENCE.removeprefix("//iam.googleapis.com/"))
    await tokens.token()
    assert parse_qs(seen[0].content.decode())["audience"] == [AUDIENCE]


async def test_token_is_cached_until_five_minutes_before_expiry():
    seen: list[httpx.Request] = []
    clock = Clock()  # fixture expiry is 13:00, so refresh is due at 12:55
    cred = FakeAzureCredential()
    tokens = provider(google(seen=seen), cred, clock)

    await tokens.token()
    clock.now += timedelta(minutes=54)
    await tokens.token()
    assert len(seen) == 2 and len(cred.scopes) == 1

    clock.now += timedelta(minutes=2)
    await tokens.token()
    assert len(seen) == 4 and len(cred.scopes) == 2


async def test_invalidate_forces_refresh():
    seen: list[httpx.Request] = []
    tokens = provider(google(seen=seen))
    await tokens.token()
    tokens.invalidate()
    await tokens.token()
    assert len(seen) == 4


@pytest.mark.parametrize(
    ("kwargs", "setting"),
    [
        ({"audience": ""}, "ACT_GCP_WIF_AUDIENCE"),
        ({"service_account": " "}, "ACT_GCP_SERVICE_ACCOUNT"),
        ({"azure_scope": ""}, "ACT_GCP_AZURE_TOKEN_SCOPE"),
    ],
)
async def test_missing_setting_is_named(kwargs, setting):
    cred = FakeAzureCredential()
    tokens = provider(google(), cred, **kwargs)
    with pytest.raises(GcpAuthError, match=f"{setting} not set"):
        await tokens.token()
    assert cred.scopes == []  # nothing was attempted


async def test_azure_failure_is_wrapped_without_token():
    cred = FakeAzureCredential(RuntimeError(f"ManagedIdentityCredential failed {AZURE_JWT}"))
    with pytest.raises(GcpAuthError, match="could not get an Azure token") as err:
        await provider(google(), cred).token()
    assert AZURE_JWT not in str(err.value)


async def test_sts_error_is_surfaced_without_tokens():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=fixture("sts_error.json"))

    with pytest.raises(GcpAuthError) as err:
        await provider(google({STS_URL: refuse})).token()
    message = str(err.value)
    assert "STS token exchange failed (400)" in message
    assert "invalid_grant" in message and "does not match the expected audience" in message
    assert AZURE_JWT not in message


async def test_impersonation_error_is_surfaced_without_tokens():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=fixture("iam_error.json"))

    with pytest.raises(GcpAuthError) as err:
        await provider(google({IMPERSONATE_URL: refuse})).token()
    message = str(err.value)
    assert f"impersonating {SA} failed (403)" in message
    assert "PERMISSION_DENIED" in message and "iam.serviceAccounts.getAccessToken" in message
    assert "ya29" not in message


async def test_error_body_echoing_a_token_is_redacted():
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad", "error_description": f"got {AZURE_JWT}"})

    with pytest.raises(GcpAuthError) as err:
        await provider(google({STS_URL: echo})).token()
    assert AZURE_JWT not in str(err.value) and "{redacted-token}" in str(err.value)


async def test_missing_access_token_in_response():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expireTime": "2026-11-04T13:00:00Z"})

    with pytest.raises(GcpAuthError, match="returned no accessToken"):
        await provider(google({IMPERSONATE_URL: empty})).token()


async def test_transport_error_is_wrapped():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(GcpAuthError, match="STS token exchange failed: ConnectError"):
        await provider(google({STS_URL: boom})).token()


async def test_close_leaves_injected_dependencies_alone():
    http = google()
    cred = FakeAzureCredential()
    tokens = provider(http, cred)
    await tokens.close()
    assert not cred.closed and not http.is_closed
    await http.aclose()
