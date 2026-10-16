"""Allocation API (#33, #34): CRUD + roles + audit, cost-centre totals == /costs/daily totals.

Reuses the M1 cost seed (tests/test_api_costs_m1.py): billed, estimated and mixed days, a
resource bill split across two same-model deployments and an unattributed meter.
"""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.api.routes.allocation import encode_resource_id
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import AllocationRule, AuditLog, SplitRule, Tenant
from ai_cost_tracker.db.session import get_session
from tests.test_api_costs_m1 import DEV, PROD, _rows

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
RANGE = {"start": "2026-10-01", "end": "2026-10-15"}


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
def principal() -> dict[str, Principal]:
    return {"p": ADMIN}


@pytest.fixture
def app(engine, principal):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: principal["p"]
    app.dependency_overrides[get_today] = lambda: date(2026, 10, 15)
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    session.add_all(_rows(tenant_id))
    await session.commit()
    return tenant_id


RULE = {
    "name": "search team",
    "priority": 10,
    "match_type": "tag",
    "match_key": "Team",
    "match_value": "search",
    "cost_centre": "CC-SEARCH",
    "team": "search",
}


# --- totals reconcile -----------------------------------------------------------------------


async def _seed_rules(session: AsyncSession, t: uuid.UUID) -> None:
    session.add_all(
        [
            AllocationRule(
                tenant_id=t, priority=10, **{k: v for k, v in RULE.items() if k != "priority"}
            ),
            AllocationRule(
                tenant_id=t,
                name="dev sub",
                priority=20,
                match_type="account",
                match_value="sub-b",
                cost_centre="CC-DEV",
                app="sandbox",
            ),
            SplitRule(
                tenant_id=t,
                resource_id=PROD,
                deployment="chat-2",
                allocations=[
                    {"cost_centre": "CC-ADS", "team": "ads", "app": "a", "percent": "33.3333"},
                    {"cost_centre": "CC-X", "team": None, "app": "b", "percent": "33.3333"},
                    {"cost_centre": "CC-Y", "team": "y", "app": "c", "percent": "33.3334"},
                ],
            ),
        ]
    )
    await session.commit()


def _daily_effective(rows: list[dict]) -> dict[str, Decimal]:
    """/costs/daily grouped by account: billed where the day is billed, else the estimate."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        field = "estimated_cost" if r["day_status"] == "estimated" else "billed_cost"
        out[r["day"]] += Decimal(r[field])
    return out


@pytest.mark.parametrize("level", ["cost_centre", "team", "app"])
@pytest.mark.parametrize(
    "filters",
    [{}, {"account_id": "sub-a"}, {"tag": "team:search"}, {"model": "gpt-4o"}],
)
async def test_cost_centre_totals_equal_costs_daily(client, session, seeded, level, filters):
    await _seed_rules(session, seeded)
    params = RANGE | filters
    costs = await client.get("/api/v1/costs/daily", params=params | {"group_by": "account"})
    cc = await client.get("/api/v1/allocation/cost-centres/daily", params=params | {"level": level})
    assert costs.status_code == cc.status_code == 200
    expected = _daily_effective(costs.json()["rows"])
    got: dict[str, Decimal] = defaultdict(Decimal)
    for r in cc.json()["rows"]:
        got[r["day"]] += Decimal(r["cost"])
    assert got == expected
    assert expected  # the seed produces cost for every filter


async def test_cost_centre_rows(client, session, seeded):
    await _seed_rules(session, seeded)
    body = (
        await client.get(
            "/api/v1/allocation/cost-centres/daily",
            params={"start": "2026-10-13", "end": "2026-10-13"},
        )
    ).json()
    assert body["currency"] == "USD"
    rows = {r["key"]: r for r in body["rows"]}
    # chat (team=search) gets 80% of 10 (shared resource -> approximate) + mini 1.
    assert rows["CC-SEARCH"] == {
        "day": "2026-10-13",
        "key": "CC-SEARCH",
        "cost": "9.0000000000",
        "approximate": True,
    }
    assert rows["CC-DEV"]["cost"] == "2.0000000000" and not rows["CC-DEV"]["approximate"]
    assert rows["unallocated"]["cost"] == "0.5000000000" and rows["unallocated"]["approximate"]
    split = [Decimal(rows[k]["cost"]) for k in ("CC-ADS", "CC-X", "CC-Y")]
    assert sum(split) == Decimal(2) and all(rows[k]["approximate"] for k in ("CC-ADS", "CC-X"))

    teams = (
        await client.get(
            "/api/v1/allocation/cost-centres/daily",
            params={"start": "2026-10-13", "end": "2026-10-13", "level": "team"},
        )
    ).json()["rows"]
    keys = {r["key"]: r["cost"] for r in teams}
    # CC-DEV rule sets no team, CC-X split part has none: both unallocated at team level.
    assert Decimal(keys["unallocated"]) == Decimal("2.5") + Decimal(rows["CC-X"]["cost"])
    assert sum(Decimal(v) for v in keys.values()) == Decimal("13.5")


async def test_cost_centre_bad_level(client, seeded):
    r = await client.get("/api/v1/allocation/cost-centres/daily", params={"level": "nope"})
    assert r.status_code == 422


async def test_untagged_endpoint(client, seeded):
    body = (await client.get("/api/v1/allocation/untagged", params=RANGE)).json()
    assert body["currency"] == "USD"
    names = [d["name"] for d in body["deployments"]]
    assert set(names) == {"chat", "chat-2", "mini", "dev"}  # none carry all four tags
    dev = next(d for d in body["deployments"] if d["name"] == "dev")
    assert dev["missing_tags"] == ["team", "app", "env", "cost-centre"]
    assert dev["account_name"] == "oai-dev"
    assert Decimal(body["total"]) == sum(Decimal(d["cost"]) for d in body["deployments"])
    assert body["unattributed_cost"] == "0.5000000000"


# --- rules CRUD -----------------------------------------------------------------------------


async def _audit(session: AsyncSession) -> list[str]:
    return [a.action for a in (await session.scalars(select(AuditLog).order_by(AuditLog.id)))]


async def test_rule_crud_and_audit(client, session, seeded):
    r = await client.post("/api/v1/allocation/rules", json=RULE)
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["match_key"] == "Team" and rule["active"] is True
    second = RULE | {"name": "acct", "match_type": "account", "match_value": "sub-b", "priority": 5}
    assert (await client.post("/api/v1/allocation/rules", json=second)).json()["match_key"] is None
    listed = (await client.get("/api/v1/allocation/rules")).json()
    assert [x["name"] for x in listed] == ["acct", "search team"]

    upd = await client.put(
        f"/api/v1/allocation/rules/{rule['id']}", json=RULE | {"cost_centre": "NEW", "app": " "}
    )
    assert upd.status_code == 200
    assert upd.json()["cost_centre"] == "NEW" and upd.json()["app"] is None

    assert (await client.delete(f"/api/v1/allocation/rules/{rule['id']}")).status_code == 204
    assert (await client.delete(f"/api/v1/allocation/rules/{rule['id']}")).status_code == 404
    missing = await client.put(f"/api/v1/allocation/rules/{uuid.uuid4()}", json=RULE)
    assert missing.status_code == 404
    assert await _audit(session) == [
        "allocation.rule_create",
        "allocation.rule_create",
        "allocation.rule_update",
        "allocation.rule_delete",
    ]


@pytest.mark.parametrize(
    "patch",
    [
        {"match_type": "regex"},
        {"match_key": None},
        {"match_type": "name_pattern", "match_value": "oai-[prod/*"},
        {"match_value": "  "},
        {"cost_centre": None, "team": None, "app": None},
        {"name": ""},
    ],
)
async def test_rule_validation(client, seeded, patch):
    r = await client.post("/api/v1/allocation/rules", json=RULE | patch)
    assert r.status_code == 422


async def test_rule_from_other_tenant_is_invisible(client, session, seeded):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    row = AllocationRule(tenant_id=other.id, name="x", match_type="account", match_value="s")
    session.add(row)
    await session.commit()
    assert (await client.get("/api/v1/allocation/rules")).json() == []
    assert (await client.delete(f"/api/v1/allocation/rules/{row.id}")).status_code == 404


async def test_viewer_reads_but_cannot_mutate(client, principal, seeded):
    principal["p"] = VIEWER
    assert (await client.get("/api/v1/allocation/rules")).status_code == 200
    assert (await client.get("/api/v1/allocation/splits")).status_code == 200
    assert (await client.post("/api/v1/allocation/rules", json=RULE)).status_code == 403
    assert (await client.delete(f"/api/v1/allocation/rules/{uuid.uuid4()}")).status_code == 403
    path = f"/api/v1/allocation/splits/{encode_resource_id(PROD)}/chat"
    body = {"allocations": [{"cost_centre": "A", "percent": 100}]}
    assert (await client.put(path, json=body)).status_code == 403
    assert (await client.delete(path)).status_code == 403


# --- splits ---------------------------------------------------------------------------------


SPLIT = {
    "allocations": [
        {"cost_centre": "A", "team": "a", "app": "x", "percent": "60.00005"},
        {"cost_centre": "B", "team": "b", "app": None, "percent": 40},
    ]
}


async def test_split_crud_and_audit(client, session, seeded):
    path = f"/api/v1/allocation/splits/{encode_resource_id(PROD)}/chat-2"
    assert (await client.get(path)).status_code == 404
    r = await client.put(path, json=SPLIT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resource_id"] == PROD and body["deployment"] == "chat-2"
    assert body["label"] == "oai-prod/chat-2" and body["approximate"] is True
    assert [a["percent"] for a in body["allocations"]] == ["60.00005", "40"]
    assert (await client.get(path)).json()["allocations"] == body["allocations"]

    again = await client.put(path, json={"allocations": [{"cost_centre": "C", "percent": 100}]})
    assert again.json()["id"] == body["id"]
    posted = await client.post(
        "/api/v1/allocation/splits", json=SPLIT | {"resource_id": DEV, "deployment": "dev"}
    )
    assert posted.status_code == 200
    listed = (await client.get("/api/v1/allocation/splits")).json()
    assert {s["label"] for s in listed} == {"oai-prod/chat-2", "oai-dev/dev"}

    assert (await client.delete(path)).status_code == 204
    assert (await client.delete(path)).status_code == 404
    assert await _audit(session) == [
        "allocation.split_create",
        "allocation.split_update",
        "allocation.split_create",
        "allocation.split_delete",
    ]


@pytest.mark.parametrize(
    "allocations",
    [
        [],
        [{"cost_centre": "A", "percent": 99.9}],
        [{"cost_centre": "A", "percent": 60}, {"cost_centre": "B", "percent": 40.001}],
        [{"cost_centre": "A", "percent": 0}, {"cost_centre": "B", "percent": 100}],
        [{"cost_centre": "A", "percent": 150}, {"cost_centre": "B", "percent": -50}],
    ],
)
async def test_split_validation(client, seeded, allocations):
    path = f"/api/v1/allocation/splits/{encode_resource_id(PROD)}/chat"
    assert (await client.put(path, json={"allocations": allocations})).status_code == 422


async def test_split_bad_resource_encoding(client, seeded):
    r = await client.get("/api/v1/allocation/splits/@@@/chat")
    assert r.status_code == 422
