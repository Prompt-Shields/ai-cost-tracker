"""CSV exports (issue #35): each file carries exactly the numbers of its JSON endpoint."""

import csv
import io
import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AllocationRule, AuditLog
from ai_cost_tracker.db.session import get_session
from tests.test_api_costs_m1 import _rows

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-13", "end": "2026-10-15"}


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
async def client(engine, session: AsyncSession, tenant_id: uuid.UUID):
    session.add_all(_rows(tenant_id))
    session.add(
        AllocationRule(
            tenant_id=tenant_id,
            name="search",
            priority=10,
            match_type="tag",
            match_key="team",
            match_value="search",
            cost_centre="CC-100",
            team="search",
        )
    )
    await session.commit()

    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: PRINCIPAL
    app.dependency_overrides[get_today] = lambda: date(2026, 10, 15)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _parse(resp: httpx.Response) -> list[dict[str, str]]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith("attachment; filename=")
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))


def _str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"group_by": "deployment"},
        {"group_by": "tag:team", "account_id": "sub-a"},
        {"model": "gpt-4o"},
    ],
)
async def test_costs_daily_matches_json(client, params):
    q = RANGE | params
    expected = (await client.get("/api/v1/costs/daily", params=q)).json()
    rows = _parse(await client.get("/api/v1/exports/costs-daily.csv", params=q))
    assert expected["rows"]
    assert [
        (r["day"], r["key"], r["billed_cost"], r["estimated_cost"], r["day_status"]) for r in rows
    ] == [
        (r["day"], r["key"], r["billed_cost"], r["estimated_cost"], r["day_status"])
        for r in expected["rows"]
    ]
    assert {r["currency"] for r in rows} == {expected["currency"]}


async def test_costs_daily_headers_and_format(client):
    resp = await client.get("/api/v1/exports/costs-daily.csv", params=RANGE)
    assert resp.headers["content-disposition"] == (
        'attachment; filename="costs-daily_2026-10-13_2026-10-15.csv"'
    )
    text = resp.content.decode("utf-8")
    assert text.splitlines()[0] == "day,key,billed_cost,estimated_cost,day_status,currency"
    # Money is a plain decimal string, never float notation.
    for row in csv.DictReader(io.StringIO(text)):
        assert "e" not in row["billed_cost"].lower()
        Decimal(row["billed_cost"])


async def test_usage_daily_matches_json(client):
    expected = (await client.get("/api/v1/usage/daily", params=RANGE)).json()["rows"]
    rows = _parse(await client.get("/api/v1/exports/usage-daily.csv", params=RANGE))
    assert expected
    assert rows == [{k: _str(v) for k, v in r.items()} for r in expected]


async def test_deployments_matches_json(client):
    expected = (await client.get("/api/v1/deployments")).json()
    resp = await client.get("/api/v1/exports/deployments.csv")
    assert resp.headers["content-disposition"] == 'attachment; filename="deployments.csv"'
    rows = _parse(resp)
    assert len(rows) == len(expected) == 4
    for row, exp in zip(rows, expected, strict=True):
        for k, v in exp.items():
            if k == "tags":
                assert json.loads(row["tags"]) == v
            else:
                assert row[k] == _str(v), k


async def test_summary_matches_json(client):
    params = RANGE | {"account_id": "sub-a"}
    expected = (await client.get("/api/v1/summary", params=params)).json()
    rows = _parse(await client.get("/api/v1/exports/summary.csv", params=params))
    assert len(rows) == len(expected["by_model"]) > 0
    for row, exp in zip(rows, expected["by_model"], strict=True):
        for k, v in exp.items():
            assert row[k] == _str(v), k
        for k in ("currency", "total_cost", "today_estimated_cost", "month_to_date_cost"):
            assert row[k] == expected[k]


@pytest.mark.parametrize("level", ["cost_centre", "team", "app"])
async def test_cost_centres_matches_allocation(client, session, tenant_id, level):
    params = RANGE | {"level": level}
    rows = _parse(await client.get("/api/v1/exports/cost-centres-daily.csv", params=params))
    resp = await client.get("/api/v1/allocation/cost-centres/daily", params=params)
    if resp.status_code == 200:  # W5's JSON endpoint, once it is in
        expected = resp.json()["rows"]
    else:
        from ai_cost_tracker.allocation import cost_centres_daily
        from ai_cost_tracker.api.deps import money

        got = await cost_centres_daily(
            session, tenant_id, date(2026, 10, 13), date(2026, 10, 15), level=level
        )
        expected = [
            {"day": r.day.isoformat(), "key": r.key, "cost": money(r.cost)}
            | {"approximate": r.approximate}
            for r in got
        ]
    assert expected
    assert [(r["day"], r["key"], r["cost"], r["approximate"]) for r in rows] == [
        (r["day"], r["key"], r["cost"], _str(r["approximate"])) for r in expected
    ]
    if level == "cost_centre":
        assert "CC-100" in {r["key"] for r in rows}


async def test_export_is_audited(client, session: AsyncSession, tenant_id: uuid.UUID):
    params = RANGE | {"group_by": "account"}
    await client.get("/api/v1/exports/costs-daily.csv", params=params)
    row = (
        await session.scalars(
            select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "export.csv")
        )
    ).one()
    assert row.actor_id == "u1"
    assert row.target_id == "costs-daily"
    assert row.details == {"export": "costs-daily", "params": params}


async def test_export_rejects_bad_params_like_json(client):
    bad = {"start": "2026-10-15", "end": "2026-10-01"}
    assert (await client.get("/api/v1/exports/costs-daily.csv", params=bad)).status_code == 422
    resp = await client.get("/api/v1/exports/costs-daily.csv", params={"group_by": "nope"})
    assert resp.status_code == 422


async def test_deployment_daily_csv_matches_deployment_page(client):
    deployments = (await client.get("/api/v1/deployments")).json()
    for dep in deployments:
        q = RANGE | {"deployment_id": dep["id"]}
        expected = (await client.get(f"/api/v1/deployments/{dep['id']}/daily", params=RANGE)).json()
        rows = _parse(await client.get("/api/v1/exports/deployment-daily.csv", params=q))
        assert (
            rows == [{k: _str(r[k]) for k in rows[0]} for r in expected["rows"]]
            if rows
            else (expected["rows"] == [])
        )
