"""Admin API: the unmapped-meter queue, admin-only."""

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import Tenant, UnmappedMeter
from ai_cost_tracker.db.session import get_session

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
URL = "/api/v1/admin/unmapped-meters"


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


def _meter(tenant_id, meter: str, cost: str) -> UnmappedMeter:
    return UnmappedMeter(
        tenant_id=tenant_id,
        cloud="azure",
        sku_meter=meter,
        first_seen=date(2026, 9, 1),
        last_seen=date(2026, 9, 30),
        total_cost=Decimal(cost),
        currency="USD",
    )


async def test_unmapped_meters_for_admin(client, session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    session.add_all(
        [
            _meter(tenant_id, "small meter", "0.5"),
            _meter(tenant_id, "big meter", "12.25"),
            _meter(other.id, "other tenant meter", "99"),
        ]
    )
    await session.commit()

    resp = await client.get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert [m["sku_meter"] for m in body] == ["big meter", "small meter"]
    assert body[0] == {
        "sku_meter": "big meter",
        "first_seen": "2026-09-01",
        "last_seen": "2026-09-30",
        "total_cost": "12.2500000000",
        "currency": "USD",
    }


async def test_unmapped_meters_owner_allowed(app, client, tenant_id):
    app.dependency_overrides[get_principal] = lambda: Principal(oid="o", roles={"owner"})
    assert (await client.get(URL)).status_code == 200


async def test_unmapped_meters_forbidden_for_viewer(app, client, tenant_id):
    app.dependency_overrides[get_principal] = lambda: VIEWER
    assert (await client.get(URL)).status_code == 403
