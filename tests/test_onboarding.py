"""Connect Azure onboarding against a mocked ARM (httpx.MockTransport)."""

import json

import httpx
import pytest

from ai_cost_tracker.connectors.azure import onboarding
from ai_cost_tracker.connectors.azure.client import ARM
from ai_cost_tracker.connectors.azure.onboarding import (
    READER_ROLE_ID,
    DelegatedTokenRejected,
    discover_subscriptions,
    grant_command,
    grant_reader,
)

TOKEN = "delegated-token"
SUB = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "99999999-9999-9999-9999-999999999999"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=ARM)


async def test_discover_one_query_with_paging_and_counts():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if "$skipToken" not in body["options"]:
            data = [{"subscriptionId": SUB, "name": "Prod", "aiAccounts": 2, "aiDeployments": 5}]
            return httpx.Response(200, json={"data": data, "$skipToken": "next"})
        data = [{"subscriptionId": "sub-2", "name": None, "aiAccounts": 0, "aiDeployments": 0}]
        return httpx.Response(200, json={"data": data})

    async with _client(handler) as http:
        subs = await discover_subscriptions(TOKEN, http)

    assert [(s.subscription_id, s.name, s.ai_accounts, s.ai_deployments) for s in subs] == [
        (SUB, "Prod", 2, 5),
        ("sub-2", "sub-2", 0, 0),
    ]
    assert len(calls) == 2
    first = calls[0]
    assert first.method == "POST"
    assert first.url.path == "/providers/Microsoft.ResourceGraph/resources"
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    query = json.loads(first.content)["query"]
    assert "microsoft.cognitiveservices/accounts/deployments" in query
    assert "'OpenAI', 'AIServices'" in query


async def test_discover_401_raises_token_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": "ExpiredAuthenticationToken", "message": "expired"}}
        )

    async with _client(handler) as http:
        with pytest.raises(DelegatedTokenRejected, match="expired"):
            await discover_subscriptions(TOKEN, http)


async def test_grant_reader_puts_role_assignment():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "x"})

    scope = f"/subscriptions/{SUB}"
    async with _client(handler) as http:
        result = await grant_reader(TOKEN, scope, PRINCIPAL, http)

    assert result == onboarding.GrantResult(scope, "granted", None)
    (req,) = seen
    assert req.method == "PUT"
    assert req.url.path.startswith(f"{scope}/providers/Microsoft.Authorization/roleAssignments/")
    assert req.url.params["api-version"] == "2022-04-01"
    props = json.loads(req.content)["properties"]
    assert props == {
        "roleDefinitionId": (
            f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{READER_ROLE_ID}"
        ),
        "principalId": PRINCIPAL,
        "principalType": "ServicePrincipal",
    }


async def test_grant_reader_management_group_scope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    scope = "/providers/Microsoft.Management/managementGroups/mg-ai"
    async with _client(handler) as http:
        result = await grant_reader(TOKEN, scope, PRINCIPAL, http)
    assert result.status == "granted"


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [(409, "RoleAssignmentExists", "already_granted"), (403, "AuthorizationFailed", "forbidden")],
)
async def test_grant_reader_maps_errors(status, code, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})

    async with _client(handler) as http:
        result = await grant_reader(TOKEN, f"/subscriptions/{SUB}", PRINCIPAL, http)
    assert result.status == expected
    assert result.detail == (None if expected == "already_granted" else "nope")


async def test_grant_reader_401_and_other_errors():
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})

    async with _client(unauthorized) as http:
        with pytest.raises(DelegatedTokenRejected):
            await grant_reader(TOKEN, f"/subscriptions/{SUB}", PRINCIPAL, http)

    def bad_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "PrincipalNotFound"}})

    async with _client(bad_request) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await grant_reader(TOKEN, f"/subscriptions/{SUB}", PRINCIPAL, http)


@pytest.mark.parametrize(
    "scope",
    [
        "/subscriptions/abc/resourceGroups/rg",
        "/subscriptions/../x",
        "https://evil.example.com/subscriptions/x",
        "/providers/Microsoft.Management/managementGroups/a/b",
    ],
)
async def test_grant_reader_rejects_bad_scope(scope):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call ARM")

    async with _client(handler) as http:
        with pytest.raises(ValueError):
            await grant_reader(TOKEN, scope, PRINCIPAL, http)


def test_grant_command():
    assert grant_command(f"/subscriptions/{SUB}", PRINCIPAL) == (
        f"az role assignment create --assignee-object-id {PRINCIPAL}"
        " --assignee-principal-type ServicePrincipal --role Reader"
        f" --scope /subscriptions/{SUB}"
    )
