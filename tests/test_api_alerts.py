"""Alerts inbox API: list with filters, resolve (admin, audited)."""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AlertEvent, AuditLog, Tenant
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
URL = "/api/v1/alerts"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
def app(engine):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: ADMIN
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    t0 = datetime(2026, 10, 15, 8, tzinfo=UTC)
    rows = [
        AlertEvent(
            tenant_id=tenant_id,
            kind="spike",
            subject="r/chat/2026-10-15",
            message="chat spiked",
            details={"ratio": "3.00"},
            severity="warning",
            created_at=t0,
        ),
        AlertEvent(
            tenant_id=tenant_id,
            kind="variance",
            subject="sub/gpt-4o/2026-10-14",
            message="variance",
            details={},
            severity="warning",
            created_at=t0.replace(hour=9),
        ),
        AlertEvent(
            tenant_id=tenant_id,
            kind="spike",
            subject="r/old/2026-10-01",
            message="old",
            details={},
            severity="critical",
            created_at=t0.replace(day=1),
            resolved_at=t0.replace(day=2),
            notified_at=t0.replace(day=1),
        ),
        AlertEvent(
            tenant_id=other.id, kind="spike", subject="x", message="other tenant", details={}
        ),
    ]
    session.add_all(rows)
    await session.commit()
    return rows


async def test_list_open_by_default_newest_first(client, seeded):
    r = await client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert [a["subject"] for a in body] == ["sub/gpt-4o/2026-10-14", "r/chat/2026-10-15"]
    first = body[1]
    assert first["id"] == str(seeded[0].id)
    assert set(first) == {
        "id",
        "kind",
        "severity",
        "subject",
        "message",
        "details",
        "created_at",
        "resolved_at",
        "notified_at",
    }
    assert first["details"] == {"ratio": "3.00"}
    assert first["created_at"].startswith("2026-10-15T08:00:00")
    assert first["resolved_at"] is None


async def test_list_filters(client, seeded):
    resolved = (await client.get(URL, params={"status": "resolved"})).json()
    assert [a["subject"] for a in resolved] == ["r/old/2026-10-01"]
    assert resolved[0]["notified_at"] is not None

    every = (await client.get(URL, params={"status": "all", "kind": "spike"})).json()
    assert {a["subject"] for a in every} == {"r/chat/2026-10-15", "r/old/2026-10-01"}

    limited = (await client.get(URL, params={"status": "all", "limit": 1})).json()
    assert len(limited) == 1

    assert (await client.get(URL, params={"status": "nope"})).status_code == 422
    assert (await client.get(URL, params={"limit": 0})).status_code == 422


async def test_viewer_can_list_but_not_resolve(app, client, seeded):
    app.dependency_overrides[get_principal] = lambda: VIEWER
    assert (await client.get(URL)).status_code == 200
    r = await client.post(f"{URL}/{seeded[0].id}/resolve")
    assert r.status_code == 403


async def test_resolve_is_audited_once(client, session, tenant_id, seeded):
    alert_id = seeded[0].id
    r = await client.post(f"{URL}/{alert_id}/resolve")
    assert r.status_code == 200
    body = r.json()
    assert body["resolved_at"] is not None
    assert body["details"]["resolved_by"] == "a1"

    again = await client.post(f"{URL}/{alert_id}/resolve")
    assert again.status_code == 200
    assert again.json()["resolved_at"] == body["resolved_at"]

    audits = list(await session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
    assert len(audits) == 1
    assert audits[0].action == "alert.resolve"
    assert audits[0].target_id == str(alert_id)
    assert audits[0].actor_id == "a1"

    open_now = (await client.get(URL)).json()
    assert [a["subject"] for a in open_now] == ["sub/gpt-4o/2026-10-14"]


async def test_resolve_unknown_or_other_tenant_is_404(client, seeded):
    assert (await client.post(f"{URL}/999999/resolve")).status_code == 404
    assert (await client.post(f"{URL}/{seeded[3].id}/resolve")).status_code == 404
