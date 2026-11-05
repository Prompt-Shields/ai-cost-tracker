"""Connect AWS endpoints with a stub connector and the SQLite schema."""

import sys
import types

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.routes.aws_connect import get_aws_connector
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.core.models import CloudAccount
from ai_cost_tracker.db.models import AuditLog, CloudAccountRow
from ai_cost_tracker.db.session import get_session
from ai_cost_tracker.worker.discovery import check_pending_access

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
TENANT = "11111111-2222-3333-4444-555555555555"
PRINCIPAL = "66666666-7777-8888-9999-000000000000"
EXPORT = "111122223333"
PROD = "123456789012"
NEW = "210987654321"


class StubAws:
    name = "stub_aws"
    cloud = "aws"

    def __init__(self) -> None:
        self.accounts = [
            CloudAccount(cloud="aws", account_id=PROD, name="AI Prod", has_ai_resources=True),
            CloudAccount(cloud="aws", account_id="999988887777", name="Website"),
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
        aws_enabled=True,
        aws_export_account_id=EXPORT,
        aws_export_bucket="act-cur",
        entra_tenant_id=TENANT,
        azure_principal_id=PRINCIPAL,
    )


@pytest.fixture
def principal() -> list[Principal]:
    return [ADMIN]


@pytest.fixture
def aws() -> StubAws:
    return StubAws()


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def app(maker, settings, principal, enqueued, aws):
    app = create_app(settings)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_principal] = lambda: principal[0]
    app.dependency_overrides[get_aws_connector] = lambda: aws
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


# --- setup + template -------------------------------------------------------------------


async def test_setup_configured(client, principal):
    principal[0] = VIEWER  # any signed-in user may read it
    resp = await client.get("/api/v1/aws/setup")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "enabled",
        "configured",
        "missing",
        "role_name",
        "export",
        "permissions",
        "template_url",
        "deploy_command_member",
        "deploy_command_export",
        "docs_path",
    }
    assert body["enabled"] is True and body["configured"] is True and body["missing"] == []
    assert body["role_name"] == "AiCostTrackerReadOnly"
    assert body["export"] == {
        "account_id": EXPORT,
        "bucket": "act-cur",
        "prefix": "ai-cost-tracker",
        "name": "ai-cost-tracker-cur2",
        "region": "us-east-1",
    }
    assert set(body["permissions"][0]) == {"permission", "why"}
    assert body["template_url"] is None
    assert body["deploy_command_member"].startswith("aws cloudformation deploy")
    assert "ExportAccount=true" in body["deploy_command_export"]
    assert "ExportAccount" not in body["deploy_command_member"]
    assert f"AzurePrincipalId={PRINCIPAL}" in body["deploy_command_member"]
    assert body["docs_path"] == "docs/aws-setup.md"


async def test_setup_not_configured(client, settings):
    settings.aws_enabled = False
    settings.aws_export_bucket = ""
    settings.aws_cfn_template_url = "https://b.s3.amazonaws.com/t.yaml"
    body = (await client.get("/api/v1/aws/setup")).json()
    assert body["enabled"] is False and body["configured"] is False
    assert body["missing"] == ["ACT_AWS_ENABLED", "ACT_AWS_EXPORT_BUCKET"]
    assert body["export"] is None
    assert body["template_url"] == "https://b.s3.amazonaws.com/t.yaml"
    assert body["deploy_command_export"].startswith("curl -fsSL")


async def test_setup_fake_mode_is_ready(client, settings):
    settings.connector = "fake"
    settings.aws_enabled = False
    settings.aws_export_account_id = ""
    settings.aws_export_bucket = ""
    settings.entra_tenant_id = ""
    body = (await client.get("/api/v1/aws/setup")).json()
    assert body["enabled"] is True and body["configured"] is True and body["missing"] == []
    assert body["export"]["account_id"] == "123456789012"
    assert body["export"]["bucket"]


async def test_template_download(client, principal):
    principal[0] = VIEWER
    resp = await client.get("/api/v1/aws/template.yaml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/yaml")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="ai-cost-tracker-readonly-role.yaml"'
    )
    assert "AWS::BCMDataExports::Export" in resp.text
    assert "__CUR_COLUMNS__" not in resp.text


async def test_template_missing_is_503(client, monkeypatch, tmp_path):
    from ai_cost_tracker.connectors.aws import onboarding

    monkeypatch.setattr(onboarding, "TEMPLATE_PATH", tmp_path / "nope.yaml")
    resp = await client.get("/api/v1/aws/template.yaml")
    assert resp.status_code == 503 and "infra/aws/readonly-role.yaml" in resp.json()["detail"]


async def test_routes_require_sign_in(app, client):
    from fastapi import HTTPException

    def unauthenticated():
        raise HTTPException(401, "sign in")

    app.dependency_overrides[get_principal] = unauthenticated
    for path in ("/api/v1/aws/setup", "/api/v1/aws/template.yaml"):
        assert (await client.get(path)).status_code == 401


# --- connector dependency ---------------------------------------------------------------


async def test_connector_dependency_503_when_disabled(settings):
    settings.aws_enabled = False
    with pytest.raises(Exception) as exc:
        await anext(get_aws_connector(settings))
    assert exc.value.status_code == 503 and "ACT_AWS_ENABLED" in exc.value.detail


async def test_connector_dependency_real_mode_builds_aws_connector(settings, monkeypatch):
    import ai_cost_tracker.api.routes.aws_connect as route
    from ai_cost_tracker.connectors import aws as aws_pkg

    built = StubAws()
    monkeypatch.setattr(aws_pkg.AwsConnector, "from_settings", classmethod(lambda cls, s: built))
    monkeypatch.setattr(
        route, "get_connector_for", lambda *a: pytest.fail("must not build Azure too")
    )
    gen = get_aws_connector(settings)
    assert await anext(gen) is built
    await gen.aclose()
    assert built.closed == 1


async def test_connector_dependency_fake_mode(settings, monkeypatch):
    import ai_cost_tracker.api.routes.aws_connect as route

    stub = StubAws()
    calls = []

    def fake_get_connector_for(s, cloud):
        calls.append(cloud)
        return stub

    monkeypatch.setattr(route, "get_connector_for", fake_get_connector_for)
    settings.connector = "fake"
    settings.aws_enabled = False
    gen = get_aws_connector(settings)
    assert await anext(gen) is stub
    await gen.aclose()
    assert calls == ["aws"] and stub.closed == 1


async def test_visible_503_through_the_route(app, client, settings):
    del app.dependency_overrides[get_aws_connector]
    settings.aws_enabled = False
    resp = await client.get("/api/v1/aws/accounts/visible")
    assert resp.status_code == 503


# --- visible accounts -------------------------------------------------------------------


async def test_visible_merges_stored_rows(client, session, tenant_id):
    session.add_all(
        [
            CloudAccountRow(tenant_id=tenant_id, cloud="aws", account_id=PROD, name="AI Prod"),
            CloudAccountRow(
                tenant_id=tenant_id,
                cloud="aws",
                account_id=NEW,
                name=NEW,
                access_status="waiting_for_access",
            ),
            # Same id on another cloud must not leak in.
            CloudAccountRow(
                tenant_id=tenant_id, cloud="azure", account_id="999988887777", name="x"
            ),
        ]
    )
    await session.commit()

    resp = await client.get("/api/v1/aws/accounts/visible")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "account_id": PROD,
            "name": "AI Prod",
            "has_ai_resources": True,
            "tracked": True,
            "access_status": "connected",
        },
        {
            "account_id": NEW,
            "name": NEW,
            "has_ai_resources": False,
            "tracked": True,
            "access_status": "waiting_for_access",
        },
        {
            "account_id": "999988887777",
            "name": "Website",
            "has_ai_resources": False,
            "tracked": False,
            "access_status": None,
        },
    ]


async def test_visible_requires_admin(client, principal):
    principal[0] = VIEWER
    assert (await client.get("/api/v1/aws/accounts/visible")).status_code == 403


async def test_visible_aws_error_is_502(client, aws):
    async def boom(include_all: bool = False):
        raise RuntimeError("AssumeRoleWithWebIdentity failed: InvalidIdentityToken\nmore")

    aws.list_accounts = boom
    resp = await client.get("/api/v1/aws/accounts/visible")
    assert resp.status_code == 502
    assert resp.json()["detail"] == (
        "AWS call failed (RuntimeError): AssumeRoleWithWebIdentity failed: InvalidIdentityToken"
    )


# --- add account ------------------------------------------------------------------------


async def test_add_connected_account(client, session, tenant_id, enqueued):
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) == {
        "account_id",
        "name",
        "access_status",
        "quick_create_url",
        "deploy_command",
    }
    assert body["account_id"] == PROD and body["name"] == PROD
    assert body["access_status"] == "connected"
    assert body["quick_create_url"] is None
    assert "ExportAccount" not in body["deploy_command"]

    row = (await _rows(session))[PROD]
    assert (row.cloud, row.tracked, row.access_status) == ("aws", True, "connected")
    assert row.tenant_id == tenant_id
    assert enqueued == [(PROD, "aws")]
    [entry] = await _audit(session)
    assert (entry.action, entry.target_type, entry.target_id) == (
        "connection.connect",
        "aws_account",
        PROD,
    )
    assert entry.details == {"cloud": "aws", "access_status": "connected", "previous": None}
    assert entry.actor_id == "a1"

    # Adding again is idempotent: one row, no second backfill.
    again = await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    assert again.json()["access_status"] == "connected"
    assert len(await _rows(session)) == 1
    assert enqueued == [(PROD, "aws")]


async def test_add_accepts_console_formatted_id(client, session, tenant_id):
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": " 1234-5678-9012 "})
    assert resp.status_code == 201 and resp.json()["account_id"] == PROD


async def test_add_export_account_gets_export_stack(client, session, tenant_id, settings):
    settings.aws_cfn_template_url = "https://b.s3.amazonaws.com/t.yaml"
    body = (await client.post("/api/v1/aws/accounts", json={"account_id": EXPORT})).json()
    assert "ExportAccount=true" in body["deploy_command"]
    assert "param_ExportAccount=true" in body["quick_create_url"]


async def test_add_waiting_then_worker_connects(
    client, session, tenant_id, aws, enqueued, maker, settings
):
    aws.access[NEW] = False
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": NEW})
    assert resp.status_code == 201
    assert resp.json()["access_status"] == "waiting_for_access"
    assert "aws cloudformation deploy" in resp.json()["deploy_command"]
    assert enqueued == []
    assert (await _rows(session))[NEW].access_status == "waiting_for_access"

    # The owner deploys the stack; the scheduled access check connects it.
    aws.access[NEW] = True
    assert await check_pending_access(maker, aws, settings) == [NEW]
    assert (await _rows(session))[NEW].access_status == "connected"


async def test_add_tracks_previously_discovered_account(client, session, tenant_id, enqueued):
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="aws",
            account_id=PROD,
            name="AI Prod",
            has_ai_resources=True,
            tracked=False,
        )
    )
    await session.commit()
    body = (await client.post("/api/v1/aws/accounts", json={"account_id": PROD})).json()
    assert body["name"] == "AI Prod"
    row = (await _rows(session))[PROD]
    assert row.tracked is True and row.has_ai_resources is True
    assert enqueued == [(PROD, "aws")]


@pytest.mark.parametrize("account_id", ["12345678901", "abcdefghijkl", "1234567890123", ""])
async def test_add_validates_account_id(client, session, tenant_id, account_id):
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": account_id})
    assert resp.status_code == 422
    assert await _rows(session) == {}
    assert await _audit(session) == []


async def test_add_requires_admin(client, session, tenant_id, principal, enqueued):
    principal[0] = VIEWER
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    assert resp.status_code == 403
    assert await _rows(session) == {} and await _audit(session) == [] and enqueued == []


async def test_add_aws_error_stores_nothing(client, session, tenant_id, aws):
    aws.access[PROD] = RuntimeError("ThrottlingException: Rate exceeded")
    resp = await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    assert resp.status_code == 502
    assert "ThrottlingException" in resp.json()["detail"]
    assert await _rows(session) == {} and await _audit(session) == []


# --- per-account setup + check ----------------------------------------------------------


async def test_account_setup_shareable(client, principal, settings):
    principal[0] = VIEWER
    settings.aws_cfn_template_url = "https://b.s3.amazonaws.com/t.yaml"
    resp = await client.get(f"/api/v1/aws/accounts/{PROD}/setup")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"account_id", "role_arn", "quick_create_url", "deploy_command"}
    assert body["role_arn"] == f"arn:aws:iam::{PROD}:role/AiCostTrackerReadOnly"
    assert body["quick_create_url"].startswith("https://us-east-1.console.aws.amazon.com/")
    assert "curl -fsSL" in body["deploy_command"]
    assert (await client.get("/api/v1/aws/accounts/NOPE/setup")).status_code == 422


async def test_check_waiting_becomes_connected(client, session, tenant_id, aws, enqueued):
    aws.access[NEW] = False
    await client.post("/api/v1/aws/accounts", json={"account_id": NEW})

    resp = await client.post(f"/api/v1/aws/accounts/{NEW}/check")
    assert resp.status_code == 200
    assert resp.json()["access_status"] == "waiting_for_access"
    assert enqueued == []

    aws.access[NEW] = True
    body = (await client.post(f"/api/v1/aws/accounts/{NEW}/check")).json()
    assert set(body) == {
        "account_id",
        "name",
        "access_status",
        "quick_create_url",
        "deploy_command",
    }
    assert body["access_status"] == "connected"
    assert enqueued == [(NEW, "aws")]

    actions = [(a.action, a.target_type, a.details["access_status"]) for a in await _audit(session)]
    assert actions == [
        ("connection.connect", "aws_account", "waiting_for_access"),
        ("connection.check", "aws_account", "waiting_for_access"),
        ("connection.check", "aws_account", "connected"),
    ]

    # Connected already: checking again doesn't start another backfill.
    await client.post(f"/api/v1/aws/accounts/{NEW}/check")
    assert enqueued == [(NEW, "aws")]


async def test_check_connected_account_that_lost_access(client, session, tenant_id, aws):
    await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    aws.access[PROD] = False
    body = (await client.post(f"/api/v1/aws/accounts/{PROD}/check")).json()
    assert body["access_status"] == "no_access"


async def test_check_unknown_account_404(client, session, tenant_id):
    assert (await client.post(f"/api/v1/aws/accounts/{NEW}/check")).status_code == 404
    assert (await client.post("/api/v1/aws/accounts/BAD/check")).status_code == 422


async def test_check_requires_admin(client, session, tenant_id, principal):
    await client.post("/api/v1/aws/accounts", json={"account_id": PROD})
    principal[0] = VIEWER
    assert (await client.post(f"/api/v1/aws/accounts/{PROD}/check")).status_code == 403
    assert len(await _audit(session)) == 1


async def test_check_aws_error_is_502_and_unchanged(client, session, tenant_id, aws):
    aws.access[NEW] = False
    await client.post("/api/v1/aws/accounts", json={"account_id": NEW})
    aws.access[NEW] = RuntimeError("botocore EndpointConnectionError")
    assert (await client.post(f"/api/v1/aws/accounts/{NEW}/check")).status_code == 502
    assert (await _rows(session))[NEW].access_status == "waiting_for_access"
    assert len(await _audit(session)) == 1
