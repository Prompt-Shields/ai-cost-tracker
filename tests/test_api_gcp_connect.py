"""Connect GCP endpoints with a stub connector and the SQLite schema."""

import sys
import types

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.routes.gcp_connect import get_gcp_connector
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.core.models import CloudAccount
from ai_cost_tracker.db.models import AuditLog, CloudAccountRow
from ai_cost_tracker.db.session import get_session
from ai_cost_tracker.worker.discovery import check_pending_access

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
SA = "act-reader@host-proj.iam.gserviceaccount.com"
TABLE = "bill-proj.billing.gcp_billing_export_resource_v1_X"


class StubGcp:
    name = "stub_gcp"
    cloud = "gcp"

    def __init__(self) -> None:
        self.accounts = [
            CloudAccount(cloud="gcp", account_id="ai-prod", name="AI Prod", has_ai_resources=True),
            CloudAccount(cloud="gcp", account_id="web-site", name="Website"),
        ]
        self.access: dict[str, bool | Exception] = {}
        self.closed = 0

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        assert include_all
        return self.accounts

    async def check_access(self, account_id: str) -> bool:
        result = self.access.get(account_id, True)
        if isinstance(result, Exception):
            raise result
        return result

    async def aclose(self) -> None:
        self.closed += 1


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
def settings() -> Settings:
    return Settings(
        environment="test",
        default_tenant_slug="internal",
        connector="azure",
        gcp_enabled=True,
        gcp_service_account=SA,
        gcp_wif_audience=(
            "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/e"
        ),
        gcp_billing_export_table=TABLE,
        entra_tenant_id="tenant-1",
        azure_principal_id="principal-1",
    )


@pytest.fixture
def principal() -> list[Principal]:
    return [ADMIN]


@pytest.fixture
def gcp() -> StubGcp:
    return StubGcp()


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def app(maker, settings, principal, enqueued, gcp):
    app = create_app(settings)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_principal] = lambda: principal[0]
    app.dependency_overrides[get_gcp_connector] = lambda: gcp
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _rows(session) -> dict[str, CloudAccountRow]:
    session.expire_all()
    return {r.account_id: r for r in await session.scalars(select(CloudAccountRow))}


async def _audit(session) -> list[AuditLog]:
    session.expire_all()
    return list(await session.scalars(select(AuditLog).order_by(AuditLog.at)))


# --- setup ------------------------------------------------------------------------------


async def test_setup_configured(client, principal):
    principal[0] = VIEWER  # any signed-in user may read it
    resp = await client.get("/api/v1/gcp/setup")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["configured"] is True and body["missing"] == []
    assert body["service_account"] == SA
    assert body["billing_export_table"] == TABLE
    assert body["docs_path"] == "docs/gcp-setup.md"
    assert {r["role"] for r in body["roles"]} >= {"roles/aiplatform.viewer"}
    assert set(body["roles"][0]) == {"role", "scope", "why"}
    assert "workload-identity-pools" in body["setup_script"]
    assert "roles/bigquery.dataViewer" in body["billing_grant_commands"]


async def test_setup_not_configured(client, settings):
    settings.gcp_enabled = False
    settings.gcp_service_account = ""
    settings.gcp_billing_export_table = ""
    body = (await client.get("/api/v1/gcp/setup")).json()
    assert body["enabled"] is False and body["configured"] is False
    assert body["missing"] == [
        "ACT_GCP_ENABLED",
        "ACT_GCP_SERVICE_ACCOUNT",
        "ACT_GCP_BILLING_EXPORT_TABLE",
    ]
    assert body["service_account"] is None
    assert body["billing_export_table"] is None and body["billing_grant_commands"] is None
    assert "AZURE_TENANT_ID=tenant-1" in body["setup_script"]


async def test_setup_fake_mode_is_ready(client, settings):
    settings.connector = "fake"
    settings.gcp_enabled = False
    settings.gcp_service_account = ""
    body = (await client.get("/api/v1/gcp/setup")).json()
    assert body["enabled"] is True and body["configured"] is True
    assert body["service_account"].endswith(".iam.gserviceaccount.com")


# --- connector dependency ---------------------------------------------------------------


async def test_connector_dependency_503s(settings):
    from ai_cost_tracker.api.routes.gcp_connect import get_gcp_connector as dep

    settings.gcp_enabled = False
    with pytest.raises(Exception) as exc:
        await anext(dep(settings))
    assert exc.value.status_code == 503 and "not enabled" in exc.value.detail

    settings.gcp_enabled = True
    settings.gcp_wif_audience = ""
    with pytest.raises(Exception) as exc:
        await anext(dep(settings))
    assert exc.value.status_code == 503 and "ACT_GCP_WIF_AUDIENCE" in exc.value.detail


async def test_connector_dependency_fake_mode(settings):
    from ai_cost_tracker.api.routes.gcp_connect import get_gcp_connector as dep

    settings.connector = "fake"
    gen = dep(settings)
    connector = await anext(gen)
    assert connector.cloud == "gcp"
    await gen.aclose()


# --- visible projects -------------------------------------------------------------------


async def test_visible_merges_stored_rows(client, session, tenant_id):
    session.add_all(
        [
            CloudAccountRow(tenant_id=tenant_id, cloud="gcp", account_id="ai-prod", name="AI Prod"),
            CloudAccountRow(
                tenant_id=tenant_id,
                cloud="gcp",
                account_id="hidden-proj",
                name="hidden-proj",
                access_status="waiting_for_access",
            ),
            # Same id on another cloud must not leak in.
            CloudAccountRow(tenant_id=tenant_id, cloud="azure", account_id="web-site", name="x"),
        ]
    )
    await session.commit()

    resp = await client.get("/api/v1/gcp/projects/visible")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "project_id": "ai-prod",
            "name": "AI Prod",
            "has_ai_resources": True,
            "tracked": True,
            "access_status": "connected",
        },
        {
            "project_id": "hidden-proj",
            "name": "hidden-proj",
            "has_ai_resources": False,
            "tracked": True,
            "access_status": "waiting_for_access",
        },
        {
            "project_id": "web-site",
            "name": "Website",
            "has_ai_resources": False,
            "tracked": False,
            "access_status": None,
        },
    ]


async def test_visible_requires_admin(client, principal):
    principal[0] = VIEWER
    assert (await client.get("/api/v1/gcp/projects/visible")).status_code == 403


async def test_visible_google_error_is_502(client, gcp):
    async def boom(include_all: bool = False):
        raise RuntimeError("STS refused the token: invalid audience\nmore")

    gcp.list_accounts = boom
    resp = await client.get("/api/v1/gcp/projects/visible")
    assert resp.status_code == 502
    assert resp.json()["detail"] == (
        "Google Cloud call failed (RuntimeError): STS refused the token: invalid audience"
    )


# --- add project ------------------------------------------------------------------------


async def test_add_connected_project(client, session, tenant_id, enqueued):
    resp = await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_id"] == "ai-prod" and body["name"] == "ai-prod"
    assert body["access_status"] == "connected"
    assert f"--member=serviceAccount:{SA} --role=roles/aiplatform.viewer" in body["grant_commands"]

    row = (await _rows(session))["ai-prod"]
    assert (row.cloud, row.tracked, row.access_status) == ("gcp", True, "connected")
    assert row.tenant_id == tenant_id
    assert enqueued == [("ai-prod", "gcp")]
    [entry] = await _audit(session)
    assert (entry.action, entry.target_type, entry.target_id) == (
        "connection.connect",
        "gcp_project",
        "ai-prod",
    )
    assert entry.details == {"cloud": "gcp", "access_status": "connected", "previous": None}
    assert entry.actor_id == "a1"

    # Adding again is idempotent: one row, no second backfill.
    assert (await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})).json()[
        "access_status"
    ] == "connected"
    assert len(await _rows(session)) == 1
    assert enqueued == [("ai-prod", "gcp")]


async def test_add_waiting_then_worker_connects(
    client, session, tenant_id, gcp, enqueued, maker, settings
):
    gcp.access["new-proj"] = False
    resp = await client.post("/api/v1/gcp/projects", json={"project_id": "new-proj"})
    assert resp.status_code == 201
    assert resp.json()["access_status"] == "waiting_for_access"
    assert "gcloud projects add-iam-policy-binding new-proj" in resp.json()["grant_commands"]
    assert enqueued == []
    assert (await _rows(session))["new-proj"].access_status == "waiting_for_access"

    # The owner runs the commands; the scheduled access check connects it.
    gcp.access["new-proj"] = True
    assert await check_pending_access(maker, gcp, settings) == ["new-proj"]
    assert (await _rows(session))["new-proj"].access_status == "connected"


async def test_add_tracks_previously_discovered_project(client, session, tenant_id, enqueued):
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="gcp",
            account_id="ai-prod",
            name="AI Prod",
            has_ai_resources=True,
            tracked=False,
        )
    )
    await session.commit()
    body = (await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})).json()
    assert body["name"] == "AI Prod"
    row = (await _rows(session))["ai-prod"]
    assert row.tracked is True and row.has_ai_resources is True
    assert enqueued == [("ai-prod", "gcp")]


@pytest.mark.parametrize("project_id", ["Bad_Project", "abc", "1234567890", "x" * 40, ""])
async def test_add_validates_project_id(client, session, tenant_id, project_id):
    resp = await client.post("/api/v1/gcp/projects", json={"project_id": project_id})
    assert resp.status_code == 422
    assert await _rows(session) == {}
    assert await _audit(session) == []


async def test_add_requires_admin(client, session, tenant_id, principal, enqueued):
    principal[0] = VIEWER
    resp = await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})
    assert resp.status_code == 403
    assert await _rows(session) == {} and await _audit(session) == [] and enqueued == []


async def test_add_google_error_stores_nothing(client, session, tenant_id, gcp):
    request = httpx.Request("GET", "https://cloudresourcemanager.googleapis.com/")
    gcp.access["ai-prod"] = httpx.HTTPStatusError(
        "500 Internal", request=request, response=httpx.Response(500, request=request)
    )
    resp = await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})
    assert resp.status_code == 502
    assert await _rows(session) == {} and await _audit(session) == []


# --- grant + check ----------------------------------------------------------------------


async def test_grant_commands_shareable(client, principal):
    principal[0] = VIEWER
    resp = await client.get("/api/v1/gcp/projects/ai-prod/grant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == "ai-prod" and body["service_account"] == SA
    assert "--role=roles/monitoring.viewer" in body["commands"]
    assert (await client.get("/api/v1/gcp/projects/NOPE/grant")).status_code == 422


async def test_grant_without_service_account_is_503(client, settings):
    settings.gcp_service_account = ""
    resp = await client.get("/api/v1/gcp/projects/ai-prod/grant")
    assert resp.status_code == 503 and "ACT_GCP_SERVICE_ACCOUNT" in resp.json()["detail"]


async def test_check_waiting_becomes_connected(client, session, tenant_id, gcp, enqueued):
    gcp.access["new-proj"] = False
    await client.post("/api/v1/gcp/projects", json={"project_id": "new-proj"})

    resp = await client.post("/api/v1/gcp/projects/new-proj/check")
    assert resp.status_code == 200
    assert resp.json()["access_status"] == "waiting_for_access"
    assert enqueued == []

    gcp.access["new-proj"] = True
    body = (await client.post("/api/v1/gcp/projects/new-proj/check")).json()
    assert set(body) == {"project_id", "name", "access_status", "grant_commands"}
    assert body["access_status"] == "connected"
    assert enqueued == [("new-proj", "gcp")]

    actions = [(a.action, a.details["access_status"]) for a in await _audit(session)]
    assert actions == [
        ("connection.connect", "waiting_for_access"),
        ("connection.check", "waiting_for_access"),
        ("connection.check", "connected"),
    ]

    # Connected already: checking again doesn't start another backfill.
    await client.post("/api/v1/gcp/projects/new-proj/check")
    assert enqueued == [("new-proj", "gcp")]


async def test_check_connected_project_that_lost_access(client, session, tenant_id, gcp):
    await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})
    gcp.access["ai-prod"] = False
    body = (await client.post("/api/v1/gcp/projects/ai-prod/check")).json()
    assert body["access_status"] == "no_access"


async def test_check_unknown_project_404(client, session, tenant_id):
    assert (await client.post("/api/v1/gcp/projects/not-added/check")).status_code == 404
    assert (await client.post("/api/v1/gcp/projects/BAD/check")).status_code == 422


async def test_check_requires_admin(client, session, tenant_id, principal):
    await client.post("/api/v1/gcp/projects", json={"project_id": "ai-prod"})
    principal[0] = VIEWER
    assert (await client.post("/api/v1/gcp/projects/ai-prod/check")).status_code == 403
    assert len(await _audit(session)) == 1
