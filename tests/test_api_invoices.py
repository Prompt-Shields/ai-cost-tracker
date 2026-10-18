"""Invoice-period endpoints (issue #36)."""

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AuditLog, CloudAccountRow, CostLine, DailyEstimate
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
TODAY = date(2026, 10, 16)


@pytest.fixture
def principal() -> list[Principal]:
    return [VIEWER]


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
    app.dependency_overrides[get_today] = lambda: TODAY
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def account(session, tenant_id):
    row = CloudAccountRow(
        tenant_id=tenant_id, cloud="azure", account_id="sub-1", name="Prod", billing_period_day=10
    )
    session.add_all(
        [
            row,
            CostLine(
                tenant_id=tenant_id,
                cloud="azure",
                sub_account_id="sub-1",
                resource_id="r1",
                charge_period_start=date(2026, 10, 9),
                sku_meter="m",
                billed_cost=Decimal("12.5"),
                billing_currency="USD",
            ),
            CostLine(
                tenant_id=tenant_id,
                cloud="azure",
                sub_account_id="sub-1",
                resource_id="r1",
                charge_period_start=date(2026, 10, 10),
                sku_meter="m",
                billed_cost=Decimal("3"),
                billing_currency="USD",
            ),
            DailyEstimate(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                resource_id="r1",
                deployment="d1",
                day=date(2026, 10, 16),
                estimated_cost=Decimal("0.5"),
            ),
        ]
    )
    await session.commit()
    return row


async def test_periods(client, account):
    resp = await client.get("/api/v1/invoices/periods", params={"account_id": "sub-1", "count": 2})
    assert resp.status_code == 200
    assert resp.json() == {
        "account_id": "sub-1",
        "billing_period_day": 10,
        "periods": [
            {
                "start": "2026-10-10",
                "end": "2026-11-09",
                "billed_cost": "3.0000000000",
                "estimated_cost": "0.5000000000",
                "currency": "USD",
                "status": "open",
            },
            {
                "start": "2026-09-10",
                "end": "2026-10-09",
                "billed_cost": "12.5000000000",
                "estimated_cost": "0.0000000000",
                "currency": "USD",
                "status": "closed",
            },
        ],
    }


async def test_periods_default_count_and_errors(client, account):
    resp = await client.get("/api/v1/invoices/periods", params={"account_id": "sub-1"})
    assert len(resp.json()["periods"]) == 6
    assert (await client.get("/api/v1/invoices/periods")).status_code == 422
    resp = await client.get("/api/v1/invoices/periods", params={"account_id": "nope"})
    assert resp.status_code == 404
    resp = await client.get("/api/v1/invoices/periods", params={"account_id": "sub-1", "count": 0})
    assert resp.status_code == 422


async def test_set_billing_period_day(client, session, account, principal):
    principal[0] = ADMIN
    url = "/api/v1/accounts/sub-1/billing-period-day"
    resp = await client.put(url, json={"billing_period_day": 28})
    assert resp.status_code == 200
    assert resp.json() == {"account_id": "sub-1", "billing_period_day": 28}
    resp = await client.put(url, json={"billing_period_day": None})
    assert resp.json()["billing_period_day"] is None

    session.expire_all()
    row = await session.scalar(select(CloudAccountRow))
    assert row.billing_period_day is None
    audits = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    assert [(a.action, a.target_id, a.details["from"], a.details["to"]) for a in audits] == [
        ("account.billing_period_day", "sub-1", 10, 28),
        ("account.billing_period_day", "sub-1", 28, None),
    ]


@pytest.mark.parametrize("body", [{"billing_period_day": 0}, {"billing_period_day": 29}, {}])
async def test_set_billing_period_day_validates(client, account, principal, body):
    principal[0] = ADMIN
    resp = await client.put("/api/v1/accounts/sub-1/billing-period-day", json=body)
    assert resp.status_code == 422


async def test_set_billing_period_day_admin_only(client, session, account):
    resp = await client.put(
        "/api/v1/accounts/sub-1/billing-period-day", json={"billing_period_day": 5}
    )
    assert resp.status_code == 403
    assert (await session.scalars(select(AuditLog))).all() == []


async def test_set_billing_period_day_unknown_account(client, account, principal):
    principal[0] = ADMIN
    resp = await client.put(
        "/api/v1/accounts/nope/billing-period-day", json={"billing_period_day": 5}
    )
    assert resp.status_code == 404
