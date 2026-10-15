"""One-click "Connect Azure" (ARCHITECTURE.md §4.3, issues #14 and #15).

Runs with the signed-in admin's *delegated* ARM token, not the app's managed identity: the
admin can see subscriptions and (if Owner / User Access Administrator) assign roles that the
app cannot yet. The token is used for the current request only and never stored or logged;
``ArmClient`` refuses to send it anywhere but management.azure.com.
"""

import re
import shlex
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ai_cost_tracker.connectors.azure.client import ARM, ArmClient

RESOURCE_GRAPH_URL = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
ROLE_ASSIGNMENTS_API_VERSION = "2022-04-01"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"  # built-in "Reader"
GRAPH_PAGE_SIZE = 1000

# Subscription ids are GUIDs; management group ids are free-form but never contain "/".
# Validating both keeps user input from reshaping the ARM path we PUT to.
_SUBSCRIPTION_SCOPE = re.compile(r"^/subscriptions/[A-Za-z0-9-]{1,64}$")
_MG_SCOPE = re.compile(
    r"^/providers/Microsoft\.Management/managementGroups/[A-Za-z0-9._()-]{1,90}$", re.IGNORECASE
)

# Visible subscriptions with counts of AI accounts and deployments, in one query.
# Assumption (#2): Resource Graph does not index ``accounts/deployments`` today, so the
# deployments join probably matches nothing and ai_deployments comes back 0. The query is
# still valid in that case; inventory sync fills real deployment counts after connecting.
_DISCOVER_QUERY = """
resourcecontainers
| where type == 'microsoft.resources/subscriptions'
| project subscriptionId, name
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.cognitiveservices/accounts' and kind in~ ('OpenAI', 'AIServices')
    | summarize aiAccounts = count() by subscriptionId
  ) on subscriptionId
| join kind=leftouter (
    resources
    | where type =~ 'microsoft.cognitiveservices/accounts/deployments'
    | summarize aiDeployments = count() by subscriptionId
  ) on subscriptionId
| project subscriptionId, name,
    aiAccounts = coalesce(aiAccounts, 0), aiDeployments = coalesce(aiDeployments, 0)
| order by subscriptionId asc
"""


class DelegatedTokenRejected(Exception):
    """ARM answered 401: the admin's delegated token is missing, expired or for another API."""


@dataclass
class SubscriptionCandidate:
    subscription_id: str
    name: str
    ai_accounts: int
    ai_deployments: int


@dataclass
class GrantResult:
    scope: str
    status: Literal["granted", "already_granted", "forbidden"]
    detail: str | None


class _StaticToken:
    """Token provider for ArmClient that hands back the delegated token as-is."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def token(self) -> str:
        return self._token


def subscription_scope(subscription_id: str) -> str:
    return validate_scope(f"/subscriptions/{subscription_id}")


def management_group_scope(group_id: str) -> str:
    return validate_scope(f"/providers/Microsoft.Management/managementGroups/{group_id}")


def validate_scope(scope: str) -> str:
    """Return ``scope`` if it is a subscription or management group scope, else ValueError."""
    if not (_SUBSCRIPTION_SCOPE.match(scope) or _MG_SCOPE.match(scope)):
        raise ValueError(f"not a subscription or management group scope: {scope!r}")
    return scope


async def discover_subscriptions(
    arm_token: str, http: httpx.AsyncClient | None = None
) -> list[SubscriptionCandidate]:
    """Every subscription the admin can see, with how many AI accounts/deployments it holds."""
    body: dict[str, Any] = {
        "query": _DISCOVER_QUERY,
        "options": {"resultFormat": "objectArray", "$top": GRAPH_PAGE_SIZE},
    }
    rows: list[dict[str, Any]] = []
    async with _client(arm_token, http) as arm:
        while True:
            payload = await _call(arm, "POST", RESOURCE_GRAPH_URL, json=body)
            rows.extend(payload.get("data") or [])
            token = payload.get("$skipToken")
            if not token:
                break
            body = {**body, "options": {**body["options"], "$skipToken": token}}
    return [
        SubscriptionCandidate(
            subscription_id=r["subscriptionId"],
            name=r.get("name") or r["subscriptionId"],
            ai_accounts=int(r.get("aiAccounts") or 0),
            ai_deployments=int(r.get("aiDeployments") or 0),
        )
        for r in rows
    ]


async def grant_reader(
    arm_token: str, scope: str, principal_id: str, http: httpx.AsyncClient | None = None
) -> GrantResult:
    """Assign built-in Reader on ``scope`` to the app's managed identity.

    Needs Owner or User Access Administrator on the scope; without it we report
    ``forbidden`` so the UI can show the ``az`` command and a share link instead.
    """
    validate_scope(scope)
    url = (
        f"{scope}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}"
        f"?api-version={ROLE_ASSIGNMENTS_API_VERSION}"
    )
    body = {
        "properties": {
            "roleDefinitionId": (
                f"{scope}/providers/Microsoft.Authorization/roleDefinitions/{READER_ROLE_ID}"
            ),
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
        }
    }
    async with _client(arm_token, http) as arm:
        try:
            await _call(arm, "PUT", url, json=body)
        except httpx.HTTPStatusError as exc:
            code, message = arm_error(exc.response)
            if exc.response.status_code == 409 and code == "RoleAssignmentExists":
                return GrantResult(scope, "already_granted", None)
            if exc.response.status_code == 403:
                return GrantResult(scope, "forbidden", message or code)
            raise
    return GrantResult(scope, "granted", None)


def grant_command(scope: str, principal_id: str) -> str:
    """The ``az`` command a subscription owner can run to grant the same role by hand."""
    return (
        "az role assignment create"
        f" --assignee-object-id {shlex.quote(principal_id)}"
        " --assignee-principal-type ServicePrincipal"
        " --role Reader"
        f" --scope {shlex.quote(scope)}"
    )


@asynccontextmanager
async def _client(token: str, http: httpx.AsyncClient | None) -> AsyncIterator[ArmClient]:
    """ArmClient around the delegated token; closes the HTTP client only if it made it."""
    owned = http is None
    http = http or httpx.AsyncClient(base_url=ARM, timeout=60)
    try:
        yield ArmClient(_StaticToken(token), http=http)
    finally:
        if owned:
            await http.aclose()


async def _call(arm: ArmClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        return await arm.request(method, url, **kwargs)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            code, message = arm_error(exc.response)
            raise DelegatedTokenRejected(message or code or "ARM rejected the token") from None
        raise


def arm_error(resp: httpx.Response) -> tuple[str | None, str | None]:
    """ARM error envelope ``{"error": {"code", "message"}}``; tolerant of other bodies."""
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        return None, None
    return err.get("code"), err.get("message")
