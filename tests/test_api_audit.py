"""Audit log admin view and the "login" row from GET /me (issue #37)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AuditLog, Tenant
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Ada Admin", roles={"admin"})
OWNER = Principal(oid="o1", name="Olga Owner", roles={"owner"})
ANALYST = Principal(oid="n1", name="Nia Analyst", roles={"analyst"})


@pytest.fixture
def principal() -> list[Principal]:
    return [ADMIN]


@pytest.fixture
def app(engine, principal):
    settings = Settings(environment="test", default_tenant_slug="internal")
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


def _row(tenant_id, at, action, actor="a1", name="Ada Admin", **kw) -> AuditLog:
    return AuditLog(
        tenant_id=tenant_id, at=at, actor_id=actor, actor_name=name, action=action, **kw
    )


async def _logins(session) -> list[AuditLog]:
    session.expire_all()
    return (await session.scalars(select(AuditLog).where(AuditLog.action == "login"))).all()


async def test_login_recorded_once_per_hour(client, session, tenant_id, principal):
    for _ in range(3):
        resp = await client.get("/api/v1/me")
        assert resp.status_code == 200 and resp.json()["oid"] == "a1"
    rows = await _logins(session)
    assert len(rows) == 1
    assert (rows[0].actor_id, rows[0].actor_name, rows[0].tenant_id) == (
        "a1",
        "Ada Admin",
        tenant_id,
    )

    principal[0] = ANALYST  # another principal gets their own row
    await client.get("/api/v1/me")
    assert sorted(r.actor_id for r in await _logins(session)) == ["a1", "n1"]


async def test_login_recorded_again_after_an_hour(client, session, tenant_id):
    session.add(_row(tenant_id, datetime.now(UTC) - timedelta(minutes=61), "login"))
    await session.commit()
    await client.get("/api/v1/me")
    assert len(await _logins(session)) == 2


async def test_login_within_hour_not_recorded(client, session, tenant_id):
    session.add(_row(tenant_id, datetime.now(UTC) - timedelta(minutes=59), "login"))
    await session.commit()
    await client.get("/api/v1/me")
    assert len(await _logins(session)) == 1


async def test_me_without_tenant_records_nothing(client, session):
    assert (await client.get("/api/v1/me")).status_code == 200
    assert (await session.scalars(select(AuditLog))).all() == []


@pytest.mark.parametrize("who", [ANALYST, Principal(oid="v", roles={"viewer"})])
async def test_audit_is_admin_only(client, tenant_id, principal, who):
    principal[0] = who
    assert (await client.get("/api/v1/audit")).status_code == 403


async def test_owner_can_read(client, tenant_id, principal):
    principal[0] = OWNER
    assert (await client.get("/api/v1/audit")).status_code == 200


async def test_list_shape_and_tenant_scope(client, session, tenant_id):
    at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    session.add_all(
        [
            _row(
                tenant_id,
                at,
                "budget.create",
                target_type="budget",
                target_id="b1",
                details={"amount": "100"},
            ),
            _row(other.id, at, "budget.create"),
        ]
    )
    await session.commit()
    body = (await client.get("/api/v1/audit")).json()
    assert body["next_before_id"] is None
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert isinstance(row["id"], str)
    assert row == {
        "id": row["id"],
        "at": "2026-10-01T12:00:00+00:00",
        "actor_id": "a1",
        "actor_name": "Ada Admin",
        "action": "budget.create",
        "target_type": "budget",
        "target_id": "b1",
        "details": {"amount": "100"},
    }


async def test_keyset_pagination(client, session, tenant_id):
    base = datetime(2026, 10, 1, tzinfo=UTC)
    session.add_all([_row(tenant_id, base + timedelta(minutes=i), f"a.{i}") for i in range(5)])
    await session.commit()

    page1 = (await client.get("/api/v1/audit", params={"limit": 2})).json()
    assert [r["action"] for r in page1["rows"]] == ["a.4", "a.3"]
    assert page1["next_before_id"] == page1["rows"][-1]["id"]
    page2 = (
        await client.get("/api/v1/audit", params={"limit": 2, "before_id": page1["next_before_id"]})
    ).json()
    assert [r["action"] for r in page2["rows"]] == ["a.2", "a.1"]
    page3 = (
        await client.get("/api/v1/audit", params={"limit": 2, "before_id": page2["next_before_id"]})
    ).json()
    assert [r["action"] for r in page3["rows"]] == ["a.0"]
    assert page3["next_before_id"] is None


async def test_filters(client, session, tenant_id):
    session.add_all(
        [
            _row(tenant_id, datetime(2026, 9, 30, 23, 59, tzinfo=UTC), "budget.create"),
            _row(tenant_id, datetime(2026, 10, 1, 0, 0, tzinfo=UTC), "budget.update"),
            _row(tenant_id, datetime(2026, 10, 2, 23, 0, tzinfo=UTC), "connection.track"),
            _row(
                tenant_id,
                datetime(2026, 10, 3, 0, 0, tzinfo=UTC),
                "budget.delete",
                actor="b2",
                name="Bob",
            ),
        ]
    )
    await session.commit()

    async def actions(**params) -> list[str]:
        resp = await client.get("/api/v1/audit", params=params)
        assert resp.status_code == 200, resp.text
        return [r["action"] for r in resp.json()["rows"]]

    assert await actions(action="budget.update") == ["budget.update"]
    assert await actions(action="budget.") == ["budget.delete", "budget.update", "budget.create"]
    assert await actions(actor="b2") == ["budget.delete"]
    assert await actions(actor="bob") == ["budget.delete"]
    assert await actions(start="2026-10-01", end="2026-10-02") == [
        "connection.track",
        "budget.update",
    ]
    assert await actions(start="2026-10-03") == ["budget.delete"]
    assert await actions(end="2026-09-30") == ["budget.create"]
    assert (
        await client.get("/api/v1/audit", params={"start": "2026-10-02", "end": "2026-10-01"})
    ).status_code == 422
    assert (await client.get("/api/v1/audit", params={"limit": 0})).status_code == 422
