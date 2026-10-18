"""PTU API (issue #32): report list and hourly drill-down."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import CostLine, DeploymentRow, PtuUtilization
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RES = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-ptu"
RANGE = {"start": "2026-10-12", "end": "2026-10-12"}


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
    app.dependency_overrides[get_principal] = lambda: PRINCIPAL
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(session, tenant_id):
    session.add_all(
        [
            DeploymentRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="s",
                resource_id=RES,
                name="batch-ptu",
                model="gpt-4o",
                sku="ProvisionedManaged",
                capacity=50,
            ),
            CostLine(
                tenant_id=tenant_id,
                cloud="azure",
                sub_account_id="s",
                resource_id=RES,
                charge_period_start=date(2026, 10, 12),
                sku_meter="Provisioned Managed Regional Unit",
                billed_cost=Decimal("1200"),
                billing_currency="USD",
            ),
            CostLine(
                tenant_id=tenant_id,
                cloud="azure",
                sub_account_id="s",
                resource_id="",
                charge_period_start=date(2026, 10, 12),
                sku_meter="Provisioned Managed Reservation",
                billed_cost=Decimal("5000"),
                billing_currency="USD",
            ),
        ]
        + [
            PtuUtilization(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="s",
                resource_id=RES,
                deployment="batch-ptu",
                bucket_start=datetime(2026, 10, 12, h, tzinfo=UTC),
                utilization=Decimal(v),
            )
            for h, v in ((9, "0.05"), (10, "0.75"))
        ]
    )
    await session.commit()


async def test_ptu_list(client, seeded):
    r = await client.get("/api/v1/ptu", params=RANGE)
    assert r.status_code == 200
    body = r.json()
    (dep,) = body["deployments"]
    assert dep == {
        "deployment_key": "oai-ptu/batch-ptu",
        "resource_id": RES,
        "account_id": "s",
        "deployment": "batch-ptu",
        "model": "gpt-4o",
        "sku": "ProvisionedManaged",
        "capacity": 50,
        "avg_utilization": "0.4000",
        "peak_utilization": "0.7500",
        "idle_hours": 1,
        "hours": 2,
        "ptu_cost": "1200.0000000000",
        "idle_cost": "720.0000000000",
        "currency": "USD",
    }
    assert body["currency"] == "USD"
    assert [x["cost"] for x in body["reservations"]] == ["5000.0000000000"]


async def test_ptu_hourly(client, seeded):
    for key in ("oai-ptu/batch-ptu", "oai-ptu%2Fbatch-ptu"):
        r = await client.get(f"/api/v1/ptu/{key}/hourly", params=RANGE)
        assert r.status_code == 200, key
        assert r.json()["rows"] == [
            {"hour": "2026-10-12T09:00:00+00:00", "utilization": "0.0500"},
            {"hour": "2026-10-12T10:00:00+00:00", "utilization": "0.7500"},
        ]


async def test_ptu_hourly_unknown_is_404(client, seeded):
    r = await client.get("/api/v1/ptu/oai-ptu/nope/hourly", params=RANGE)
    assert r.status_code == 404


async def test_ptu_empty_without_tenant(client):
    r = await client.get("/api/v1/ptu", params=RANGE)
    assert r.status_code == 200
    assert r.json()["deployments"] == []


async def test_ptu_requires_principal(app, client):
    app.dependency_overrides.pop(get_principal)
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test", default_tenant_slug="internal", entra_tenant_id="t", entra_client_id="c"
    )
    r = await client.get("/api/v1/ptu", params=RANGE)
    assert r.status_code in (401, 403)
