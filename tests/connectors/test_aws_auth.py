"""AwsSessions: Azure token -> unsigned AssumeRoleWithWebIdentity -> per-account clients,
caching and refresh, error mapping, pagination. No network: botocore Stubber everywhere."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from ai_cost_tracker.connectors.aws.auth import (
    CLIENT_CONFIG,
    AwsAccessDenied,
    AwsAuthError,
    AwsSessions,
    is_access_denied,
    redact,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "aws" / "auth"
SCOPE = "api://AzureADTokenExchange/.default"
ROLE = "AiCostTrackerReadOnly"
ACCOUNT = "111111111111"
OTHER = "222222222222"
AZURE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJtaSJ9.c2lnbmF0dXJl"
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def assume_response(key_id: str = "ASIAFAKEFAKEFAKE0001", expires: datetime | None = None):
    body = fixture("assume_role_with_web_identity.json")
    body.pop("_comment")
    creds = body["Credentials"]
    creds["AccessKeyId"] = key_id
    creds["Expiration"] = expires or datetime.fromisoformat(creds["Expiration"])
    return body


def assume_params(account: str = ACCOUNT) -> dict[str, Any]:
    return {
        "RoleArn": f"arn:aws:iam::{account}:role/{ROLE}",
        "RoleSessionName": "ai-cost-tracker",
        "WebIdentityToken": AZURE_JWT,
        "DurationSeconds": 3600,
    }


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
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def sts():
    client = boto3.client("sts", region_name="us-east-1", config=Config(signature_version=UNSIGNED))
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def make(sts_client, *, credential=None, clock=None, role=ROLE, scope=SCOPE) -> AwsSessions:
    return AwsSessions(
        role_name=role,
        azure_scope=scope,
        azure_credential=credential or FakeAzureCredential(),
        sts_client=sts_client,
        clock=clock or Clock(),
    )


def access_key(client) -> str:
    return client._request_signer._credentials.access_key


# --- credentials -----------------------------------------------------------------------------


async def test_client_assumes_role_with_azure_token(sts):
    client, stub = sts
    stub.add_response("assume_role_with_web_identity", assume_response(), assume_params())
    cred = FakeAzureCredential()
    sessions = make(client, credential=cred)

    s3 = await sessions.client("s3", ACCOUNT, "us-east-1")

    assert cred.scopes == [SCOPE]
    assert s3.meta.service_model.service_name == "s3"
    assert s3.meta.region_name == "us-east-1"
    assert access_key(s3) == "ASIAFAKEFAKEFAKE0001"
    creds = s3._request_signer._credentials
    assert creds.token == "fake-session-token"
    assert s3.meta.config.retries == CLIENT_CONFIG.retries
    assert s3.meta.config.retries["mode"] == "standard"
    assert s3.meta.config.connect_timeout == 10
    assert s3.meta.config.read_timeout == 60
    await sessions.aclose()


async def test_clients_and_credentials_are_cached_until_shortly_before_expiry(sts):
    client, stub = sts
    clock = Clock()
    stub.add_response("assume_role_with_web_identity", assume_response(), assume_params())
    sessions = make(client, clock=clock)

    first = await sessions.client("cloudwatch", ACCOUNT, "us-east-1")
    clock.now = T0 + timedelta(minutes=54)
    assert await sessions.client("cloudwatch", ACCOUNT, "us-east-1") is first
    # Another service or region reuses the credentials (no second STS call queued).
    other = await sessions.client("bedrock", ACCOUNT, "eu-west-1")
    assert access_key(other) == access_key(first)

    # Expiry 13:00, margin 5 min: at 12:55 the credentials are refreshed.
    clock.now = T0 + timedelta(minutes=55)
    stub.add_response(
        "assume_role_with_web_identity",
        assume_response("ASIAFAKEFAKEFAKE0002", T0 + timedelta(hours=2)),
        assume_params(),
    )
    fresh = await sessions.client("cloudwatch", ACCOUNT, "us-east-1")
    assert fresh is not first
    assert access_key(fresh) == "ASIAFAKEFAKEFAKE0002"
    await sessions.aclose()


async def test_concurrent_callers_share_one_assume(sts):
    client, stub = sts
    stub.add_response("assume_role_with_web_identity", assume_response(), assume_params())
    cred = FakeAzureCredential()
    sessions = make(client, credential=cred)

    got = await asyncio.gather(
        *(sessions.client("cloudwatch", ACCOUNT, "us-east-1") for _ in range(5))
    )

    assert len({id(c) for c in got}) == 1
    assert cred.scopes == [SCOPE]
    await sessions.aclose()


async def test_accounts_are_isolated(sts):
    client, stub = sts
    stub.add_response(
        "assume_role_with_web_identity", assume_response("ASIAFAKEFAKEFAKE000A"), assume_params()
    )
    stub.add_response(
        "assume_role_with_web_identity",
        assume_response("ASIAFAKEFAKEFAKE000B"),
        assume_params(OTHER),
    )
    sessions = make(client)

    a = await sessions.client("s3", ACCOUNT, "us-east-1")
    b = await sessions.client("s3", OTHER, "us-east-1")

    assert a is not b
    assert access_key(a) == "ASIAFAKEFAKEFAKE000A"
    assert access_key(b) == "ASIAFAKEFAKEFAKE000B"
    await sessions.aclose()


async def test_missing_expiration_falls_back_to_requested_duration():
    class NoExpiry:
        calls = 0

        def assume_role_with_web_identity(self, **_: Any):
            self.calls += 1
            creds = dict(assume_response()["Credentials"])
            creds.pop("Expiration")
            return {"Credentials": creds}

    clock, sts_client = Clock(), NoExpiry()
    sessions = make(sts_client, clock=clock)
    await sessions.client("s3", ACCOUNT, "us-east-1")
    clock.now = T0 + timedelta(minutes=54)
    await sessions.client("s3", ACCOUNT, "us-east-1")
    assert sts_client.calls == 1
    clock.now = T0 + timedelta(minutes=55)
    await sessions.client("s3", ACCOUNT, "us-east-1")
    assert sts_client.calls == 2
    await sessions.aclose()


async def test_default_sts_client_is_unsigned_and_regional():
    sessions = AwsSessions(
        role_name=ROLE, azure_scope=SCOPE, azure_credential=FakeAzureCredential()
    )
    sts_client = await sessions._sts_client()
    assert sts_client.meta.config.signature_version is UNSIGNED
    assert sts_client.meta.endpoint_url == "https://sts.us-east-1.amazonaws.com"
    await sessions.aclose()
    assert sessions._sts is None


# --- credential errors -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["AccessDenied", "InvalidIdentityToken", "IDPRejectedClaim", "ExpiredTokenException"]
)
async def test_sts_refusal_is_access_denied(sts, code):
    client, stub = sts
    stub.add_client_error(
        "assume_role_with_web_identity",
        service_error_code=code,
        service_message=f"Not authorized to perform sts:AssumeRoleWithWebIdentity {AZURE_JWT}",
        http_status_code=403,
        expected_params=assume_params(),
    )
    sessions = make(client)

    with pytest.raises(AwsAccessDenied) as err:
        await sessions.client("s3", ACCOUNT, "us-east-1")

    text = str(err.value)
    assert code in text
    assert f"arn:aws:iam::{ACCOUNT}:role/{ROLE}" in text
    assert AZURE_JWT not in text
    assert err.value.__cause__ is None


async def test_other_sts_errors_are_auth_errors(sts):
    client, stub = sts
    stub.add_client_error(
        "assume_role_with_web_identity",
        service_error_code="RegionDisabledException",
        service_message="STS is not activated in this region",
        http_status_code=403,
    )
    sessions = make(client)

    with pytest.raises(AwsAuthError, match="RegionDisabledException"):
        await sessions.client("s3", ACCOUNT, "us-east-1")


async def test_sts_transport_failure_is_auth_error():
    class Broken:
        def assume_role_with_web_identity(self, **_: Any):
            raise EndpointConnectionError(endpoint_url="https://sts.us-east-1.amazonaws.com")

    sessions = make(Broken())
    with pytest.raises(AwsAuthError, match="EndpointConnectionError"):
        await sessions.client("s3", ACCOUNT, "us-east-1")


async def test_sts_without_credentials_is_auth_error():
    class Empty:
        def assume_role_with_web_identity(self, **_: Any):
            return {"Credentials": {"AccessKeyId": "ASIAFAKEFAKEFAKE0001"}}

    sessions = make(Empty())
    with pytest.raises(AwsAuthError, match="returned no credentials") as err:
        await sessions.client("s3", ACCOUNT, "us-east-1")
    assert "ASIAFAKEFAKEFAKE0001" not in str(err.value)


async def test_azure_token_failure_is_scrubbed_auth_error(sts):
    client, _ = sts
    cred = FakeAzureCredential(RuntimeError(f"ManagedIdentityCredential failed: {AZURE_JWT}"))
    sessions = make(client, credential=cred)

    with pytest.raises(AwsAuthError) as err:
        await sessions.client("s3", ACCOUNT, "us-east-1")

    text = str(err.value)
    assert "ACT_AWS_AZURE_TOKEN_SCOPE" in text
    assert SCOPE in text
    assert AZURE_JWT not in text
    assert "{redacted-token}" in text


@pytest.mark.parametrize(
    ("role", "scope", "setting"),
    [("", SCOPE, "ACT_AWS_ROLE_NAME"), (ROLE, " ", "ACT_AWS_AZURE_TOKEN_SCOPE")],
)
async def test_missing_settings_named(sts, role, scope, setting):
    client, _ = sts
    cred = FakeAzureCredential()
    sessions = make(client, credential=cred, role=role, scope=scope)

    with pytest.raises(AwsAuthError, match=setting):
        await sessions.client("s3", ACCOUNT, "us-east-1")
    assert cred.scopes == []


async def test_invalid_account_id_rejected(sts):
    client, _ = sts
    with pytest.raises(ValueError, match="12-digit"):
        await make(client).client("s3", "12345", "us-east-1")


# --- call / paginate -------------------------------------------------------------------------


async def stubbed(sessions: AwsSessions, sts_stub: Stubber, service: str, region="us-east-1"):
    sts_stub.add_response("assume_role_with_web_identity", assume_response(), assume_params())
    created = await sessions.client(service, ACCOUNT, region)
    stub = Stubber(created)
    stub.activate()
    return created, stub


async def test_call_returns_response(sts):
    client, sts_stub = sts
    sessions = make(client)
    cw, stub = await stubbed(sessions, sts_stub, "cloudwatch")
    stub.add_response(
        "list_metrics",
        {"Metrics": [{"Namespace": "AWS/Bedrock", "MetricName": "InputTokenCount"}]},
        {"Namespace": "AWS/Bedrock"},
    )

    got = await sessions.call(cw, "list_metrics", Namespace="AWS/Bedrock")

    assert got["Metrics"][0]["MetricName"] == "InputTokenCount"
    stub.assert_no_pending_responses()


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException"])
async def test_call_maps_access_denied(sts, code):
    client, sts_stub = sts
    sessions = make(client)
    s3, stub = await stubbed(sessions, sts_stub, "s3")
    stub.add_client_error(
        "get_object",
        service_error_code=code,
        service_message="User: arn:aws:sts::111111111111:assumed-role/x is not authorized",
        http_status_code=403,
    )

    with pytest.raises(AwsAccessDenied, match=rf"s3\.get_object denied \({code}\)"):
        await sessions.call(s3, "get_object", Bucket="b", Key="k")


async def test_call_reraises_other_client_errors(sts):
    client, sts_stub = sts
    sessions = make(client)
    s3, stub = await stubbed(sessions, sts_stub, "s3")
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with pytest.raises(ClientError) as err:
        await sessions.call(s3, "get_object", Bucket="b", Key="k")
    assert err.value.response["Error"]["Code"] == "NoSuchKey"


@pytest.mark.parametrize(
    ("code", "denied"),
    [
        ("AccessDenied", True),
        ("AccessDeniedException", True),
        ("UnauthorizedOperation", True),
        ("AuthorizationError", True),
        ("NotAuthorized", True),
        ("OperationNotAuthorizedException", True),
        ("ThrottlingException", False),
        ("ValidationException", False),
        ("ResourceNotFoundException", False),
    ],
)
def test_is_access_denied(code, denied):
    assert is_access_denied(code) is denied


async def test_paginate_uses_native_paginator(sts):
    client, sts_stub = sts
    sessions = make(client)
    s3, stub = await stubbed(sessions, sts_stub, "s3")
    first, second = fixture("list_objects_v2_pages.json")
    stub.add_response("list_objects_v2", first, {"Bucket": "exports", "Prefix": "ai-cost-tracker/"})
    stub.add_response(
        "list_objects_v2",
        second,
        {"Bucket": "exports", "Prefix": "ai-cost-tracker/", "ContinuationToken": "page-2"},
    )

    items = await sessions.paginate(
        s3, "list_objects_v2", "Contents", Bucket="exports", Prefix="ai-cost-tracker/"
    )

    assert [i["Key"].rsplit("/", 1)[1] for i in items] == [
        "part-0.csv.gz",
        "part-1.csv.gz",
        "manifest.json",
    ]
    stub.assert_no_pending_responses()


async def test_paginate_native_maps_access_denied(sts):
    client, sts_stub = sts
    sessions = make(client)
    s3, stub = await stubbed(sessions, sts_stub, "s3")
    stub.add_client_error(
        "list_objects_v2", service_error_code="AccessDenied", http_status_code=403
    )

    with pytest.raises(AwsAccessDenied, match="list_objects_v2"):
        await sessions.paginate(s3, "list_objects_v2", "Contents", Bucket="exports")


class TokenClient:
    """A client whose operation has no botocore paginator."""

    def __init__(self, pages: list[dict], token_name: str, can_paginate: bool | None) -> None:
        self.pages = pages
        self.token_name = token_name
        self.calls: list[dict] = []
        if can_paginate is not None:
            self.can_paginate = lambda method: can_paginate

    def list_things(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


@pytest.mark.parametrize(
    ("token_name", "can_paginate"), [("NextToken", False), ("nextToken", None)]
)
async def test_paginate_falls_back_to_token_loop(token_name, can_paginate):
    pages = [
        {"items": [{"id": 1}, {"id": 2}], token_name: "t1"},
        {"items": [{"id": 3}], token_name: "t2"},
        {"items": []},
    ]
    fake = TokenClient(pages, token_name, can_paginate)
    sessions = make(object())

    items = await sessions.paginate(fake, "list_things", "items", maxResults=2)

    assert [i["id"] for i in items] == [1, 2, 3]
    assert fake.calls == [
        {"maxResults": 2},
        {"maxResults": 2, token_name: "t1"},
        {"maxResults": 2, token_name: "t2"},
    ]


async def test_paginate_token_loop_stops_on_repeated_token():
    pages = [
        {"items": [{"id": 1}], "NextToken": "same"},
        {"items": [{"id": 2}], "NextToken": "same"},
    ]
    fake = TokenClient(pages, "NextToken", False)
    items = await make(object()).paginate(fake, "list_things", "items")
    assert [i["id"] for i in items] == [1, 2]


async def test_paginate_token_loop_maps_access_denied():
    class Denied:
        def list_things(self, **_: Any):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListThings"
            )

    with pytest.raises(AwsAccessDenied, match=r"list_things denied \(AccessDeniedException\)"):
        await make(object()).paginate(Denied(), "list_things", "items")


# --- lifecycle / hygiene ---------------------------------------------------------------------


async def test_aclose_closes_clients_but_not_injected_credential(sts):
    client, sts_stub = sts
    cred = FakeAzureCredential()
    sessions = make(client, credential=cred)
    sts_stub.add_response("assume_role_with_web_identity", assume_response(), assume_params())
    await sessions.client("s3", ACCOUNT, "us-east-1")

    await sessions.aclose()

    assert sessions._accounts == {}
    assert cred.closed is False
    assert sessions._sts is client


def test_redact_strips_tokens_and_key_ids():
    text = redact(f"token {AZURE_JWT} key ASIAFAKEFAKEFAKE0001 and AKIAABCDEFGHIJKLMNOP")
    assert AZURE_JWT not in text
    assert "ASIA" not in text and "AKIA" not in text
    assert text.count("{redacted-key}") == 2
