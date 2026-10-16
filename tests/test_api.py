"""Read API against the SQLite schema, with auth and the session dependency overridden."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DeploymentRow,
    Tenant,
    TokenUsageRow,
)
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-01", "end": "2026-10-02"}


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
    app.dependency_overrides[get_today] = lambda: date(2026, 10, 2)
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _cost(tenant_id, day, meter, model, cost, currency="USD") -> CostLine:
    return CostLine(
        tenant_id=tenant_id,
        cloud="azure",
        sub_account_id="sub-1",
        resource_id="/sub/rg/openai-1",
        charge_period_start=day,
        service_name="Cognitive Services",
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency=currency,
        ai_model=model,
    )


def _usage(tenant_id, hour, deployment, model, inp, out, req) -> TokenUsageRow:
    return TokenUsageRow(
        tenant_id=tenant_id,
        cloud="azure",
        account_id="sub-1",
        resource_id="/sub/rg/openai-1",
        deployment=deployment,
        model=model,
        bucket_start=hour,
        input_tokens=inp,
        output_tokens=out,
        requests=req,
    )


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    # Rows of another tenant must never leak into results.
    other_tenant = Tenant(slug="other", name="Other")
    session.add(other_tenant)
    await session.flush()
    other = other_tenant.id
    session.add_all(
        [
            CloudAccountRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                name="Prod",
                has_ai_resources=True,
                last_synced_at=datetime(2026, 10, 2, 8, 0, tzinfo=UTC),
            ),
            CloudAccountRow(tenant_id=tenant_id, cloud="azure", account_id="sub-2", name="Dev"),
            DeploymentRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                resource_id="/sub/rg/openai-1",
                name="chat",
                model="gpt-4o",
                model_version="2024-08-06",
                sku="GlobalStandard",
                capacity=100,
                tags={"team": "search"},
            ),
            DeploymentRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                resource_id="/sub/rg/openai-1",
                name="old",
                model="gpt-35-turbo",
                deleted_at=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            _cost(tenant_id, date(2026, 10, 1), "gpt-4o In", "gpt-4o", "1.50"),
            _cost(tenant_id, date(2026, 10, 1), "gpt-4o Out", "gpt-4o", "2.25"),
            _cost(tenant_id, date(2026, 10, 1), "mini In", "gpt-4o-mini", "0.10"),
            _cost(tenant_id, date(2026, 10, 2), "gpt-4o In", "gpt-4o", "3"),
            _cost(tenant_id, date(2026, 10, 2), "Mystery meter", None, "0.05"),
            _cost(tenant_id, date(2026, 10, 3), "gpt-4o In", "gpt-4o", "100"),  # out of range
            _cost(other, date(2026, 10, 1), "gpt-4o In", "gpt-4o", "999"),
            _usage(tenant_id, datetime(2026, 10, 1, 0, tzinfo=UTC), "chat", "gpt-4o", 1000, 500, 3),
            _usage(tenant_id, datetime(2026, 10, 1, 23, tzinfo=UTC), "chat", "gpt-4o", 0, 500, 1),
            _usage(
                tenant_id, datetime(2026, 10, 2, 5, tzinfo=UTC), "chat", "gpt-4o", 500000, 0, 10
            ),
            _usage(tenant_id, datetime(2026, 10, 3, 0, tzinfo=UTC), "chat", "gpt-4o", 9, 9, 9),
            _usage(other, datetime(2026, 10, 1, 1, tzinfo=UTC), "chat", "gpt-4o", 7, 7, 7),
        ]
    )
    await session.commit()
    return tenant_id


async def test_healthz_needs_no_auth(app, client):
    def deny():
        raise HTTPException(401, "no token")

    app.dependency_overrides[get_principal] = deny
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/me",
        "/api/v1/accounts",
        "/api/v1/deployments",
        "/api/v1/costs/daily",
        "/api/v1/usage/daily",
        "/api/v1/summary",
    ],
)
async def test_auth_errors_propagate(app, client, path):
    def deny():
        raise HTTPException(401, "no token", headers={"WWW-Authenticate": "Bearer"})

    app.dependency_overrides[get_principal] = deny
    resp = await client.get(path)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_me(client):
    resp = await client.get("/api/v1/me")
    assert resp.json() == {
        "oid": "u1",
        "name": "Ada",
        "email": "ada@example.com",
        "groups": [],
        "roles": ["viewer"],
    }


async def test_accounts(client, seeded):
    resp = await client.get("/api/v1/accounts")
    assert resp.json() == [
        {
            "cloud": "azure",
            "account_id": "sub-2",
            "name": "Dev",
            "has_ai_resources": False,
            "last_synced_at": None,
        },
        {
            "cloud": "azure",
            "account_id": "sub-1",
            "name": "Prod",
            "has_ai_resources": True,
            "last_synced_at": "2026-10-02T08:00:00+00:00",
        },
    ]


async def test_deployments_excludes_deleted(client, seeded):
    resp = await client.get("/api/v1/deployments")
    body = resp.json()
    assert len(body) == 1
    uuid.UUID(body[0].pop("id"))
    assert body == [
        {
            "cloud": "azure",
            "account_id": "sub-1",
            "account_name": "Prod",
            "resource_id": "/sub/rg/openai-1",
            "name": "chat",
            "model": "gpt-4o",
            "model_version": "2024-08-06",
            "sku": "GlobalStandard",
            "capacity": 100,
            "region": None,
            "tags": {"team": "search"},
        }
    ]


def _daily(day: str, model: str, billed: str) -> dict:
    return {
        "day": day,
        "key": model,
        "model": model,
        "billed_cost": billed,
        "estimated_cost": "0.0000000000",
        "day_status": "billed",
    }


async def test_costs_daily(client, seeded):
    resp = await client.get("/api/v1/costs/daily", params=RANGE)
    assert resp.json() == {
        "currency": "USD",
        "rows": [
            _daily("2026-10-01", "gpt-4o", "3.7500000000"),
            _daily("2026-10-01", "gpt-4o-mini", "0.1000000000"),
            _daily("2026-10-02", "gpt-4o", "3.0000000000"),
            _daily("2026-10-02", "unmapped", "0.0500000000"),
        ],
    }


async def test_usage_daily_buckets_by_utc_day(client, seeded):
    resp = await client.get("/api/v1/usage/daily", params=RANGE)
    assert resp.json() == {
        "rows": [
            {
                "day": "2026-10-01",
                "deployment": "chat",
                "model": "gpt-4o",
                "input_tokens": 1000,
                "output_tokens": 1000,
                "requests": 4,
            },
            {
                "day": "2026-10-02",
                "deployment": "chat",
                "model": "gpt-4o",
                "input_tokens": 500000,
                "output_tokens": 0,
                "requests": 10,
            },
        ]
    }


async def test_summary(client, seeded):
    resp = await client.get("/api/v1/summary", params=RANGE)
    assert resp.json() == {
        "currency": "USD",
        "total_cost": "6.9000000000",
        "today_estimated_cost": "0.0000000000",
        "month_to_date_cost": "6.9000000000",
        "by_model": [
            {
                "model": "gpt-4o",
                "billed_cost": "6.7500000000",
                "estimated_cost": "0.0000000000",
                "input_tokens": 501000,
                "output_tokens": 1000,
                # 6.75 / 502000 * 1e6
                "cost_per_1m_tokens": "13.4462",
                # These cost lines carry no direction, so nothing counts as input/output.
                "cost_per_1m_input": "0.0000",
                "cost_per_1m_output": "0.0000",
            },
            {
                "model": "gpt-4o-mini",
                "billed_cost": "0.1000000000",
                "estimated_cost": "0.0000000000",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_per_1m_tokens": None,
                "cost_per_1m_input": None,
                "cost_per_1m_output": None,
            },
            {
                "model": "unmapped",
                "billed_cost": "0.0500000000",
                "estimated_cost": "0.0000000000",
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_per_1m_tokens": None,
                "cost_per_1m_input": None,
                "cost_per_1m_output": None,
            },
        ],
    }


async def test_missing_tenant_returns_empty(client):
    # No tenant row at all: empty results, nothing created.
    assert (await client.get("/api/v1/accounts")).json() == []
    assert (await client.get("/api/v1/costs/daily", params=RANGE)).json() == {
        "currency": "USD",
        "rows": [],
    }
    assert (await client.get("/api/v1/summary", params=RANGE)).json() == {
        "currency": "USD",
        "total_cost": "0.0000000000",
        "today_estimated_cost": "0.0000000000",
        "month_to_date_cost": "0.0000000000",
        "by_model": [],
    }


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-02", "end": "2026-10-01"},
        {"start": "2025-01-01", "end": "2026-02-05"},  # 401 days
        {"start": "not-a-date"},
    ],
)
@pytest.mark.parametrize("path", ["/api/v1/costs/daily", "/api/v1/usage/daily", "/api/v1/summary"])
async def test_range_validation(client, path, params):
    assert (await client.get(path, params=params)).status_code == 422


async def test_max_range_allowed(client):
    resp = await client.get("/api/v1/summary", params={"start": "2025-01-01", "end": "2026-02-04"})
    assert resp.status_code == 200


async def test_cors_only_for_local():
    local = create_app(Settings(environment="local"))
    test = create_app(Settings(environment="test"))
    headers = {"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"}
    for app, expected in ((local, "http://localhost:3000"), (test, None)):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.options("/healthz", headers=headers)
        assert resp.headers.get("access-control-allow-origin") == expected
