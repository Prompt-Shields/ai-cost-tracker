"""Connection health panel and discovered-subscription banner endpoints."""

import sys
import types
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import CloudAccountRow, SyncState
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", roles={"admin"})
VIEWER = Principal(oid="v1", roles={"viewer"})
PRINCIPAL_ID = "pid-1"


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
def principal() -> list[Principal]:
    return [VIEWER]


@pytest.fixture
def app(engine, principal, enqueued):
    settings = Settings(
        environment="test", default_tenant_slug="internal", azure_principal_id=PRINCIPAL_ID
    )
    app = create_app(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_principal] = lambda: principal[0]
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _account(tenant_id, account_id, name, **kw) -> CloudAccountRow:
    return CloudAccountRow(
        tenant_id=tenant_id, cloud="azure", account_id=account_id, name=name, **kw
    )


async def test_list_connections_with_health_and_backfill(client, session, tenant_id):
    now = datetime.now(UTC)
    session.add_all(
        [
            _account(tenant_id, "sub-1", "Prod", has_ai_resources=True),
            _account(tenant_id, "sub-2", "Waiting", access_status="waiting_for_access"),
            SyncState(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                source="usage",
                last_success_at=now - timedelta(minutes=5),
            ),
            SyncState(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                source="cost",
                last_success_at=now - timedelta(hours=1),
                last_error="Client error '403 Forbidden' for url ...",
                backfill_from=date(2025, 10, 1),
                backfill_done_through=date(2026, 4, 1),
                backfill_progress=Decimal("0.5"),
            ),
        ]
    )
    await session.commit()

    resp = await client.get("/api/v1/connections")
    assert resp.status_code == 200
    by_id = {c["account_id"]: c for c in resp.json()}
    prod = by_id["sub-1"]
    assert prod["cloud"] == "azure" and prod["tracked"] and prod["access_status"] == "connected"
    assert prod["sources"]["usage"]["status"] == "ok"
    assert prod["sources"]["inventory"]["status"] == "never"
    cost = prod["sources"]["cost"]
    assert cost["status"] == "error"
    assert cost["message"] == "Reader role missing on subscription sub-1"
    assert "--assignee-object-id pid-1" in cost["fix"]
    assert cost["last_success_at"].endswith("+00:00")
    assert prod["backfill"] == {
        "cost": {"progress": 0.5, "from": "2025-10-01", "done_through": "2026-04-01"},
        "usage": None,
    }
    waiting = by_id["sub-2"]
    assert {s["status"] for s in waiting["sources"].values()} == {"never"}
    assert waiting["sources"]["cost"]["message"] == "Waiting for someone to grant access"


async def test_connections_scoped_to_tenant(client, session, tenant_id):
    from ai_cost_tracker.db.models import Tenant

    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.commit()
    session.add(_account(other.id, "sub-x", "Elsewhere"))
    await session.commit()
    assert (await client.get("/api/v1/connections")).json() == []


async def test_discovered(client, session, tenant_id):
    seen = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    session.add_all(
        [
            _account(
                tenant_id, "new", "New AI", tracked=False, has_ai_resources=True, discovered_at=seen
            ),
            _account(tenant_id, "empty", "No AI", tracked=False, has_ai_resources=False),
            _account(tenant_id, "tracked", "Tracked", tracked=True, has_ai_resources=True),
        ]
    )
    await session.commit()
    resp = await client.get("/api/v1/connections/discovered")
    assert resp.json() == [
        {"account_id": "new", "name": "New AI", "discovered_at": "2026-10-14T06:00:00+00:00"}
    ]


async def test_track_requires_admin(client, session, tenant_id):
    session.add(_account(tenant_id, "new", "New", tracked=False, has_ai_resources=True))
    await session.commit()
    resp = await client.post("/api/v1/connections/new/track", json={"tracked": True})
    assert resp.status_code == 403


async def test_track_enqueues_backfill(client, session, tenant_id, principal, enqueued):
    principal[0] = ADMIN
    session.add_all(
        [
            _account(tenant_id, "new", "New", tracked=False, has_ai_resources=True),
            _account(tenant_id, "wait", "Wait", tracked=False, access_status="waiting_for_access"),
        ]
    )
    await session.commit()

    resp = await client.post("/api/v1/connections/new/track", json={"tracked": True})
    assert resp.status_code == 200
    assert resp.json()["tracked"] is True
    assert resp.json()["account_id"] == "new"
    assert enqueued == [("new", "azure")]

    resp = await client.post("/api/v1/connections/wait/track", json={"tracked": True})
    assert resp.status_code == 200
    assert enqueued == [("new", "azure")]

    resp = await client.post("/api/v1/connections/new/track", json={"tracked": False})
    assert resp.json()["tracked"] is False
    assert len(enqueued) == 1
    assert (await client.get("/api/v1/connections/discovered")).json()[0]["account_id"] == "new"

    resp = await client.post("/api/v1/connections/missing/track", json={"tracked": True})
    assert resp.status_code == 404


async def test_track_and_untrack_write_audit_rows(client, session, tenant_id, principal):
    from sqlalchemy import select

    from ai_cost_tracker.db.models import AuditLog

    principal[0] = ADMIN
    session.add(_account(tenant_id, "new", "New", tracked=False, has_ai_resources=True))
    await session.commit()
    await client.post("/api/v1/connections/new/track", json={"tracked": True})
    await client.post("/api/v1/connections/new/track", json={"tracked": False})
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    assert [(r.action, r.target_id, r.actor_id) for r in rows] == [
        ("connection.track", "new", "a1"),
        ("connection.untrack", "new", "a1"),
    ]


async def test_viewer_track_writes_no_audit_row(client, session, tenant_id):
    from sqlalchemy import select

    from ai_cost_tracker.db.models import AuditLog

    session.add(_account(tenant_id, "new", "New", tracked=False))
    await session.commit()
    assert (
        await client.post("/api/v1/connections/new/track", json={"tracked": True})
    ).status_code == 403
    assert (await session.scalars(select(AuditLog))).all() == []
