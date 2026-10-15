"""Connect Azure endpoints with a mocked ARM and the SQLite schema."""

import sys
import types

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.routes.azure_connect import TOKEN_HEADER, get_arm_http
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors.azure.client import ARM
from ai_cost_tracker.db.models import CloudAccountRow
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
PRINCIPAL_ID = "99999999-9999-9999-9999-999999999999"
SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"
HEADERS = {TOKEN_HEADER: "delegated"}

GRAPH_ROWS = [
    {"subscriptionId": SUB_A, "name": "Prod AI", "aiAccounts": 1, "aiDeployments": 3},
    {"subscriptionId": SUB_B, "name": "Dev", "aiAccounts": 0, "aiDeployments": 0},
]


class Arm:
    """Mock ARM: Resource Graph returns GRAPH_ROWS; role assignments answer per scope."""

    def __init__(self) -> None:
        self.grants: dict[str, tuple[int, dict]] = {}
        self.graph_status = 200
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/Microsoft.ResourceGraph/resources"):
            if self.graph_status != 200:
                return httpx.Response(self.graph_status, json={"error": {"code": "Expired"}})
            return httpx.Response(200, json={"data": GRAPH_ROWS})
        scope = request.url.path.split("/providers/Microsoft.Authorization/")[0]
        status, body = self.grants.get(scope, (201, {}))
        return httpx.Response(status, json=body)

    def puts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "PUT"]


@pytest.fixture
def enqueued(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def enqueue_backfill(account_id: str, cloud: str = "azure") -> None:
        calls.append((account_id, cloud))

    module = types.ModuleType("ai_cost_tracker.worker.enqueue")
    module.enqueue_backfill = enqueue_backfill
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.worker.enqueue", module)
    return calls


@pytest.fixture
def arm() -> Arm:
    return Arm()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        default_tenant_slug="internal",
        azure_principal_id=PRINCIPAL_ID,
        web_base_url="https://costs.example.com/",
    )


@pytest.fixture
def principal() -> list[Principal]:
    return [ADMIN]


@pytest.fixture
def app(engine, arm, settings, principal, enqueued):
    app = create_app(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    async def arm_http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(arm), base_url=ARM) as c:
            yield c

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_principal] = lambda: principal[0]
    app.dependency_overrides[get_arm_http] = arm_http
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _accounts(session) -> dict[str, CloudAccountRow]:
    session.expire_all()
    return {r.account_id: r for r in await session.scalars(select(CloudAccountRow))}


async def test_discover_merges_db_state(client, session, tenant_id, arm):
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id=SUB_A,
            name="Prod AI",
            tracked=True,
            access_status="waiting_for_access",
        )
    )
    await session.commit()

    resp = await client.post("/api/v1/azure/discover", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {
        "subscriptions": [
            {
                "subscription_id": SUB_A,
                "name": "Prod AI",
                "ai_accounts": 1,
                "ai_deployments": 3,
                "tracked": True,
                "access_status": "waiting_for_access",
            },
            {
                "subscription_id": SUB_B,
                "name": "Dev",
                "ai_accounts": 0,
                "ai_deployments": 0,
                "tracked": None,
                "access_status": None,
            },
        ]
    }
    assert arm.requests[0].headers["Authorization"] == "Bearer delegated"


@pytest.mark.parametrize("path", ["/api/v1/azure/discover", "/api/v1/azure/connect"])
async def test_missing_header_is_400(client, path):
    resp = await client.post(path, json={"subscription_ids": [SUB_A]})
    assert resp.status_code == 400
    assert TOKEN_HEADER in resp.json()["detail"]


@pytest.mark.parametrize("path", ["/api/v1/azure/discover", "/api/v1/azure/connect"])
async def test_viewer_is_forbidden(client, principal, path):
    principal[0] = VIEWER
    resp = await client.post(path, headers=HEADERS, json={"subscription_ids": [SUB_A]})
    assert resp.status_code == 403


async def test_rejected_token_is_502(client, arm):
    arm.graph_status = 401
    resp = await client.post("/api/v1/azure/discover", headers=HEADERS)
    assert resp.status_code == 502
    assert "rejected your sign-in token" in resp.json()["detail"]
    assert "delegated" not in resp.text


async def test_connect_grants_and_tracks(client, session, tenant_id, arm, enqueued):
    arm.grants[f"/subscriptions/{SUB_B}"] = (
        403,
        {"error": {"code": "AuthorizationFailed", "message": "no rights"}},
    )
    resp = await client.post(
        "/api/v1/azure/connect", headers=HEADERS, json={"subscription_ids": [SUB_A, SUB_B]}
    )
    assert resp.status_code == 200
    results = {r["subscription_id"]: r for r in resp.json()["results"]}
    assert results[SUB_A]["status"] == "connected"
    assert results[SUB_B]["status"] == "waiting_for_access"
    assert results[SUB_B]["command"] == (
        f"az role assignment create --assignee-object-id {PRINCIPAL_ID}"
        f" --assignee-principal-type ServicePrincipal --role Reader --scope /subscriptions/{SUB_B}"
    )
    assert results[SUB_B]["share_url"] == f"https://costs.example.com/connect/pending/{SUB_B}"

    rows = await _accounts(session)
    assert rows[SUB_A].tracked and rows[SUB_A].access_status == "connected"
    assert rows[SUB_A].name == "Prod AI" and rows[SUB_A].has_ai_resources
    assert rows[SUB_A].discovered_at is not None
    assert rows[SUB_B].access_status == "waiting_for_access"
    assert rows[SUB_A].tenant_id == tenant_id
    assert enqueued == [(SUB_A, "azure")]
    assert len(arm.puts()) == 2


async def test_connect_already_granted_is_connected(client, session, tenant_id, arm, enqueued):
    arm.grants[f"/subscriptions/{SUB_A}"] = (409, {"error": {"code": "RoleAssignmentExists"}})
    resp = await client.post(
        "/api/v1/azure/connect", headers=HEADERS, json={"subscription_ids": [SUB_A]}
    )
    assert resp.json()["results"][0]["status"] == "connected"
    assert enqueued == [(SUB_A, "azure")]


async def test_connect_keeps_existing_connected_on_forbidden(client, session, tenant_id, arm):
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id=SUB_A,
            name="Old",
            tracked=False,
            access_status="connected",
        )
    )
    await session.commit()
    arm.grants[f"/subscriptions/{SUB_A}"] = (403, {"error": {"code": "AuthorizationFailed"}})
    resp = await client.post(
        "/api/v1/azure/connect", headers=HEADERS, json={"subscription_ids": [SUB_A]}
    )
    assert resp.json()["results"][0]["status"] == "connected"
    row = (await _accounts(session))[SUB_A]
    assert row.tracked and row.access_status == "connected" and row.name == "Prod AI"


async def test_connect_management_group_single_grant(client, session, tenant_id, arm, enqueued):
    mg = "/providers/Microsoft.Management/managementGroups/mg-ai"
    arm.grants[mg] = (403, {"error": {"code": "AuthorizationFailed"}})
    resp = await client.post(
        "/api/v1/azure/connect",
        headers=HEADERS,
        json={"subscription_ids": [SUB_A, SUB_B], "management_group_id": "mg-ai"},
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert {r["status"] for r in results} == {"waiting_for_access"}
    assert all(r["command"].endswith(f"--scope {mg}") for r in results)
    assert [r.url.path.split("/providers/Microsoft.Authorization/")[0] for r in arm.puts()] == [mg]
    assert enqueued == []


async def test_connect_requires_principal_id(client, settings):
    settings.azure_principal_id = ""
    resp = await client.post(
        "/api/v1/azure/connect", headers=HEADERS, json={"subscription_ids": [SUB_A]}
    )
    assert resp.status_code == 500
    assert "ACT_AZURE_PRINCIPAL_ID" in resp.json()["detail"]


async def test_connect_rejects_bad_subscription_id(client, arm):
    resp = await client.post(
        "/api/v1/azure/connect",
        headers=HEADERS,
        json={"subscription_ids": ["x/resourceGroups/rg"]},
    )
    assert resp.status_code == 422
    assert arm.requests == []


async def test_connect_rejected_token_on_grant_is_502(client, arm):
    arm.grants[f"/subscriptions/{SUB_A}"] = (401, {"error": {"code": "InvalidAuthenticationToken"}})
    resp = await client.post(
        "/api/v1/azure/connect", headers=HEADERS, json={"subscription_ids": [SUB_A]}
    )
    assert resp.status_code == 502


async def test_pending(client, session, tenant_id, principal):
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id=SUB_B,
            name="Dev",
            access_status="waiting_for_access",
        )
    )
    await session.commit()
    principal[0] = VIEWER  # the owner opening the share link needn't be an admin here
    resp = await client.get(f"/api/v1/azure/pending/{SUB_B}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Dev" and body["status"] == "waiting_for_access"
    assert f"--scope /subscriptions/{SUB_B}" in body["command"]
    assert body["share_url"] == f"https://costs.example.com/connect/pending/{SUB_B}"

    assert (await client.get(f"/api/v1/azure/pending/{SUB_A}")).status_code == 404
