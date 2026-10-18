"""Budgets API (issue #29): CRUD, validation, role checks and audit rows."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AuditLog, Budget, DailyEstimate, DeploymentRow, Tenant
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", roles={"viewer"})
TODAY = date(2026, 10, 15)
RES = "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/r"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
def principal() -> list[Principal]:
    return [ADMIN]


@pytest.fixture
def app(engine, principal):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: principal[0]
    app.dependency_overrides[get_today] = lambda: TODAY
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def deployment(session, tenant_id) -> DeploymentRow:
    dep = DeploymentRow(
        tenant_id=tenant_id,
        cloud="azure",
        account_id="sub-a",
        resource_id=RES,
        name="chat",
        model="gpt-4o",
        tags={"team": "search"},
    )
    session.add(dep)
    day = date(2026, 9, 25)
    while day <= TODAY:
        session.add(
            DailyEstimate(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-a",
                resource_id=RES,
                deployment="chat",
                model="gpt-4o",
                day=day,
                input_cost=Decimal(10),
                output_cost=Decimal(0),
                estimated_cost=Decimal(10),
                currency="USD",
            )
        )
        day += timedelta(days=1)
    await session.commit()
    return dep


async def _audit(session) -> list[AuditLog]:
    return list((await session.scalars(select(AuditLog).order_by(AuditLog.id))).all())


async def test_crud_with_status_and_audit(client, session, tenant_id, deployment):
    body = {
        "name": "Chat",
        "scope_type": "deployment",
        "scope_value": str(deployment.id),
        "amount": "200",
        "thresholds": [1.0, 0.8],
    }
    resp = await client.post("/api/v1/budgets", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["amount"] == "200.0000000000"
    assert created["thresholds"] == [0.8, 1.0]
    assert created["month_to_date"] == "150.0000000000"  # 10-01..10-15 at 10/day
    assert created["forecast_month_end"] == "310.0000000000"  # + 16 days x 10
    assert created["percent_used"] == 75.0
    assert created["status"] == "warning"
    assert created["currency"] == "USD" and created["active"] is True

    listed = (await client.get("/api/v1/budgets")).json()
    assert [b["id"] for b in listed] == [created["id"]]
    assert set(listed[0]) == {
        "id", "name", "scope_type", "scope_value", "amount", "currency", "thresholds",
        "active", "month_to_date", "forecast_month_end", "percent_used", "status",
    }  # fmt: skip

    resp = await client.put(
        f"/api/v1/budgets/{created['id']}",
        json={"name": "All", "scope_type": "all", "scope_value": "ignored", "amount": "100"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["scope_value"] is None and updated["status"] == "over"

    assert (await client.delete(f"/api/v1/budgets/{created['id']}")).status_code == 204
    assert (await client.get("/api/v1/budgets")).json() == []
    assert (await client.delete(f"/api/v1/budgets/{created['id']}")).status_code == 404

    rows = await _audit(session)
    assert [r.action for r in rows] == ["budget.create", "budget.update", "budget.delete"]
    assert all(r.target_id == created["id"] and r.actor_id == "a1" for r in rows)
    assert rows[0].details["amount"] == "200.0000000000"
    assert rows[1].details["changes"]["scope_type"] == {"from": "deployment", "to": "all"}


@pytest.mark.parametrize(
    "patch",
    [
        {"amount": "0"},
        {"amount": "-5"},
        {"thresholds": [0]},
        {"thresholds": [2.5]},
        {"thresholds": []},
        {"scope_type": "account", "scope_value": None},
        {"scope_type": "tag", "scope_value": "nocolon"},
        {"scope_type": "tag", "scope_value": ":value"},
        {"scope_type": "deployment", "scope_value": "not-a-uuid"},
        {"scope_type": "deployment", "scope_value": str(uuid.uuid4())},
        {"scope_type": "nope", "scope_value": "x"},
        {"name": ""},
        {"currency": "usd"},
    ],
)
async def test_validation(client, session, tenant_id, patch):
    body = {"name": "B", "scope_type": "all", "amount": "100"} | patch
    resp = await client.post("/api/v1/budgets", json=body)
    assert resp.status_code == 422, resp.text
    assert await _audit(session) == []


async def test_deployment_of_other_tenant_rejected(client, session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    dep = DeploymentRow(
        tenant_id=other.id, cloud="azure", account_id="x", resource_id=RES, name="d", model="m"
    )
    session.add(dep)
    await session.commit()
    body = {"name": "B", "scope_type": "deployment", "scope_value": str(dep.id), "amount": "1"}
    assert (await client.post("/api/v1/budgets", json=body)).status_code == 422


async def test_tag_and_account_scopes(client, tenant_id, deployment):
    for scope_type, value in (("tag", "team:search"), ("account", "sub-a"), ("model", "gpt-4o")):
        body = {
            "name": scope_type,
            "scope_type": scope_type,
            "scope_value": value,
            "amount": "1000",
        }
        resp = await client.post("/api/v1/budgets", json=body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["month_to_date"] == "150.0000000000"
        assert resp.json()["status"] == "ok"


async def test_viewer_can_read_but_not_write(client, session, tenant_id, principal):
    budget = Budget(tenant_id=tenant_id, name="B", scope_type="all", amount=Decimal(5))
    session.add(budget)
    await session.commit()
    principal[0] = VIEWER
    assert (await client.get("/api/v1/budgets")).status_code == 200
    body = {"name": "B", "scope_type": "all", "amount": "100"}
    assert (await client.post("/api/v1/budgets", json=body)).status_code == 403
    assert (await client.put(f"/api/v1/budgets/{budget.id}", json=body)).status_code == 403
    assert (await client.delete(f"/api/v1/budgets/{budget.id}")).status_code == 403
    assert await _audit(session) == []


async def test_other_tenants_budget_is_404(client, session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    budget = Budget(tenant_id=other.id, name="B", scope_type="all", amount=Decimal(5))
    session.add(budget)
    await session.commit()
    assert (await client.get("/api/v1/budgets")).json() == []
    body = {"name": "B", "scope_type": "all", "amount": "100"}
    assert (await client.put(f"/api/v1/budgets/{budget.id}", json=body)).status_code == 404
    assert (await client.delete(f"/api/v1/budgets/{budget.id}")).status_code == 404
    assert (await client.delete("/api/v1/budgets/not-a-uuid")).status_code == 404


async def test_create_without_tenant_creates_it(client, session):
    body = {"name": "B", "scope_type": "all", "amount": "100"}
    resp = await client.post("/api/v1/budgets", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["month_to_date"] == "0.0000000000"
    assert resp.json()["status"] == "ok"
