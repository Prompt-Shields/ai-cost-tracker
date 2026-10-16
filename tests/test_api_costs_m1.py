"""M1 cost API (issue #22): billed vs estimated mixing, group_by, filters, deployment detail.

Seed (today = 2026-10-15, two accounts, other tenant's rows must never show up):

- oai-prod (sub-a): deployments chat (gpt-4o, team=search), chat-2 (gpt-4o, team=ads),
  mini (gpt-4o-mini, team=search). oai-dev (sub-b): dev (gpt-4o, no tags).
- 10-13: billed for both accounts; Reconciliation says sub-a is finalised.
- 10-14: billed for sub-a (cost lines; its stale recon row still says estimated), sub-b only
  estimated.
- 10-15 (today): estimates only.
- 10-13 gpt-4o on oai-prod is split chat/chat-2 by tokens (400k/100k -> 80/20); the
  unparseable meter stays on the resource as "unattributed" (and untagged: its deployments
  disagree on team).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    Reconciliation,
    Tenant,
    TokenUsageRow,
)
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-13", "end": "2026-10-15"}
_ARM = "resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts"
PROD = f"/subscriptions/sub-a/{_ARM}/oai-prod"
DEV = f"/subscriptions/sub-b/{_ARM}/oai-dev"
Z = "0.0000000000"


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
    app.dependency_overrides[get_today] = lambda: date(2026, 10, 15)
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _dep(t, account, resource, name, model, tags, **kw) -> DeploymentRow:
    return DeploymentRow(
        tenant_id=t,
        cloud="azure",
        account_id=account,
        resource_id=resource,
        name=name,
        model=model,
        tags=tags,
        **kw,
    )


def _cost(t, account, resource, day, meter, model, direction, cost) -> CostLine:
    return CostLine(
        tenant_id=t,
        cloud="azure",
        sub_account_id=account,
        resource_id=resource,
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
        ai_model=model,
        ai_direction=direction,
    )


def _usage(t, account, resource, deployment, model, hour, inp, out) -> TokenUsageRow:
    return TokenUsageRow(
        tenant_id=t,
        cloud="azure",
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model=model,
        bucket_start=hour,
        input_tokens=inp,
        output_tokens=out,
        requests=1,
    )


def _est(t, account, resource, deployment, model, day, inp_cost, out_cost) -> DailyEstimate:
    return DailyEstimate(
        tenant_id=t,
        cloud="azure",
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model=model,
        day=day,
        input_cost=Decimal(inp_cost),
        output_cost=Decimal(out_cost),
        estimated_cost=Decimal(inp_cost) + Decimal(out_cost),
        currency="USD",
    )


def _recon(t, account, model, day, status) -> Reconciliation:
    return Reconciliation(
        tenant_id=t, cloud="azure", account_id=account, model=model, day=day, day_status=status
    )


D01, D13, D14, D15 = date(2026, 10, 1), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)


def _h(day: date, hour: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def _rows(t) -> list:
    return [
        CloudAccountRow(tenant_id=t, cloud="azure", account_id="sub-a", name="Prod"),
        CloudAccountRow(tenant_id=t, cloud="azure", account_id="sub-b", name="Dev"),
        _dep(t, "sub-a", PROD, "chat", "gpt-4o", {"team": "search"}, region="eastus2"),
        _dep(t, "sub-a", PROD, "chat-2", "gpt-4o", {"team": "ads"}),
        _dep(t, "sub-a", PROD, "mini", "gpt-4o-mini", {"team": "search"}),
        _dep(t, "sub-b", DEV, "dev", "gpt-4o", {}),
        # Billed
        _cost(t, "sub-a", PROD, date(2026, 9, 30), "gpt-4o In", "gpt-4o", "input", "100"),
        _cost(t, "sub-a", PROD, D01, "gpt-4o In", "gpt-4o", "input", "10"),
        _cost(t, "sub-a", PROD, D13, "gpt-4o In", "gpt-4o", "input", "4"),
        _cost(t, "sub-a", PROD, D13, "gpt-4o Out", "gpt-4o", "output", "6"),
        _cost(t, "sub-a", PROD, D13, "mini In", "gpt-4o-mini", "input", "1"),
        _cost(t, "sub-a", PROD, D13, "Mystery", None, None, "0.5"),
        _cost(t, "sub-b", DEV, D13, "gpt-4o In", "gpt-4o", "input", "2"),
        _cost(t, "sub-a", PROD, D14, "gpt-4o In", "gpt-4o", "input", "3"),
        _cost(t, "sub-a", PROD, D14, "gpt-4o Cached", "gpt-4o", "cached_input", "1"),
        # Tokens
        _usage(t, "sub-a", PROD, "chat", "gpt-4o", _h(D13), 300_000, 0),
        _usage(t, "sub-a", PROD, "chat", "gpt-4o", _h(D13, 23), 0, 100_000),
        _usage(t, "sub-a", PROD, "chat-2", "gpt-4o", _h(D13), 100_000, 0),
        _usage(t, "sub-a", PROD, "mini", "gpt-4o-mini", _h(D13), 1_000_000, 0),
        _usage(t, "sub-b", DEV, "dev", "gpt-4o", _h(D13), 1_000_000, 0),
        _usage(t, "sub-a", PROD, "chat", "gpt-4o", _h(D14), 1_000_000, 0),
        _usage(t, "sub-b", DEV, "dev", "gpt-4o", _h(D14), 500_000, 0),
        _usage(t, "sub-a", PROD, "chat", "gpt-4o", _h(D15), 200_000, 0),
        # Estimates
        _est(t, "sub-a", PROD, "chat", "gpt-4o", D01, "7", "0"),
        _est(t, "sub-a", PROD, "chat", "gpt-4o", D13, "4", "5"),
        _est(t, "sub-a", PROD, "chat-2", "gpt-4o", D13, "1", "0"),
        _est(t, "sub-a", PROD, "mini", "gpt-4o-mini", D13, "0.9", "0"),
        _est(t, "sub-b", DEV, "dev", "gpt-4o", D13, "2.2", "0"),
        _est(t, "sub-a", PROD, "chat", "gpt-4o", D14, "3.9", "0"),
        _est(t, "sub-b", DEV, "dev", "gpt-4o", D14, "1.5", "0"),
        _est(t, "sub-a", PROD, "chat", "gpt-4o", D15, "0.7", "0"),
        # Reconciliation
        _recon(t, "sub-a", "gpt-4o", D13, "finalised"),
        _recon(t, "sub-a", "gpt-4o", D14, "estimated"),  # stale: cost lines say billed
        _recon(t, "sub-b", "gpt-4o", D14, "estimated"),
    ]


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    o = other.id
    session.add_all(_rows(tenant_id))
    session.add_all(
        [
            _dep(o, "sub-a", PROD, "chat", "gpt-4o", {"team": "search"}),
            _cost(o, "sub-a", PROD, D15, "gpt-4o In", "gpt-4o", "input", "999"),
            _est(o, "sub-b", DEV, "dev", "gpt-4o", D15, "999", "0"),
            _usage(o, "sub-a", PROD, "chat", "gpt-4o", _h(D13), 9, 9),
            _recon(o, "sub-b", "gpt-4o", D14, "finalised"),
        ]
    )
    await session.commit()
    deps = (await session.execute(DeploymentRow.__table__.select())).all()
    ids = {f"{r.tenant_id == tenant_id}/{r.name}": str(r.id) for r in deps}
    return {"chat": ids["True/chat"], "dev": ids["True/dev"], "other": ids["False/chat"]}


def _row(day, key, billed, estimated, status, **extra) -> dict:
    return {
        "day": f"2026-10-{day}",
        "key": key,
        **extra,
        "billed_cost": billed,
        "estimated_cost": estimated,
        "day_status": status,
    }


def _m(day, key, billed, estimated, status) -> dict:
    return _row(day, key, billed, estimated, status, model=key)


async def _daily(client, **params) -> dict:
    resp = await client.get("/api/v1/costs/daily", params=RANGE | params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_daily_by_model_mixes_billed_and_estimated(client, seeded):
    assert await _daily(client) == {
        "currency": "USD",
        "rows": [
            # sub-a finalised, sub-b billed: the row is as settled as its least settled part.
            _m("13", "gpt-4o", "12.0000000000", "12.2000000000", "billed"),
            _m("13", "gpt-4o-mini", "1.0000000000", "0.9000000000", "finalised"),
            _m("13", "unmapped", "0.5000000000", Z, "finalised"),
            # sub-b has no billing for 10-14 yet.
            _m("14", "gpt-4o", "4.0000000000", "5.4000000000", "estimated"),
            _m("15", "gpt-4o", Z, "0.7000000000", "estimated"),
        ],
    }


async def test_daily_by_account(client, seeded):
    assert (await _daily(client, group_by="account"))["rows"] == [
        _row("13", "sub-a", "11.5000000000", "10.9000000000", "finalised"),
        _row("13", "sub-b", "2.0000000000", "2.2000000000", "billed"),
        _row("14", "sub-a", "4.0000000000", "3.9000000000", "billed"),
        _row("14", "sub-b", Z, "1.5000000000", "estimated"),
        _row("15", "sub-a", Z, "0.7000000000", "estimated"),
    ]


async def test_daily_by_deployment_splits_resource_cost(client, seeded):
    assert (await _daily(client, group_by="deployment"))["rows"] == [
        _row("13", "oai-dev/dev", "2.0000000000", "2.2000000000", "billed"),
        _row("13", "oai-prod/chat", "8.0000000000", "9.0000000000", "finalised"),
        _row("13", "oai-prod/chat-2", "2.0000000000", "1.0000000000", "finalised"),
        _row("13", "oai-prod/mini", "1.0000000000", "0.9000000000", "finalised"),
        _row("13", "oai-prod/unattributed", "0.5000000000", Z, "finalised"),
        _row("14", "oai-dev/dev", Z, "1.5000000000", "estimated"),
        # chat-2 had no tokens on 10-14, so chat carries all of it.
        _row("14", "oai-prod/chat", "4.0000000000", "3.9000000000", "billed"),
        _row("15", "oai-prod/chat", Z, "0.7000000000", "estimated"),
    ]


async def test_daily_by_tag(client, seeded):
    assert (await _daily(client, group_by="tag:team"))["rows"] == [
        _row("13", "ads", "2.0000000000", "1.0000000000", "finalised"),
        _row("13", "search", "9.0000000000", "9.9000000000", "finalised"),
        _row("13", "untagged", "2.5000000000", "2.2000000000", "billed"),
        _row("14", "search", "4.0000000000", "3.9000000000", "billed"),
        _row("14", "untagged", Z, "1.5000000000", "estimated"),
        _row("15", "search", Z, "0.7000000000", "estimated"),
    ]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"tag": "team:search"},
            [
                _m("13", "gpt-4o", "8.0000000000", "9.0000000000", "finalised"),
                _m("13", "gpt-4o-mini", "1.0000000000", "0.9000000000", "finalised"),
                _m("14", "gpt-4o", "4.0000000000", "3.9000000000", "billed"),
                _m("15", "gpt-4o", Z, "0.7000000000", "estimated"),
            ],
        ),
        (
            {"tag": "team:ads"},
            [_m("13", "gpt-4o", "2.0000000000", "1.0000000000", "finalised")],
        ),
        ({"tag": "team:nobody"}, []),
        (
            {"account_id": "sub-b"},
            [
                _m("13", "gpt-4o", "2.0000000000", "2.2000000000", "billed"),
                _m("14", "gpt-4o", Z, "1.5000000000", "estimated"),
            ],
        ),
        ({"model": "unmapped"}, [_m("13", "unmapped", "0.5000000000", Z, "finalised")]),
        (
            {"model": "gpt-4o-mini", "account_id": "sub-a"},
            [_m("13", "gpt-4o-mini", "1.0000000000", "0.9000000000", "finalised")],
        ),
        ({"model": "gpt-4o-mini", "account_id": "sub-b"}, []),
    ],
)
async def test_daily_filters(client, seeded, params, expected):
    assert (await _daily(client, **params))["rows"] == expected


async def test_summary(client, seeded):
    resp = await client.get("/api/v1/summary", params=RANGE)
    assert resp.json() == {
        "currency": "USD",
        # billed 10-13 (13.5) + sub-a billed 10-14 (4) + sub-b est 10-14 (1.5) + est 10-15 (0.7)
        "total_cost": "19.7000000000",
        "today_estimated_cost": "0.7000000000",
        # + 10-01 billed 10 (its estimate of 7 is superseded); 09-30 is last month.
        "month_to_date_cost": "29.7000000000",
        "by_model": [
            {
                "model": "gpt-4o",
                "billed_cost": "16.0000000000",
                "estimated_cost": "18.3000000000",
                "input_tokens": 3_100_000,
                "output_tokens": 100_000,
                # effective 18.2 / 3.2M; input 12.2 / 3.1M; output 6 / 0.1M
                "cost_per_1m_tokens": "5.6875",
                "cost_per_1m_input": "3.9355",
                "cost_per_1m_output": "60.0000",
            },
            {
                "model": "gpt-4o-mini",
                "billed_cost": "1.0000000000",
                "estimated_cost": "0.9000000000",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cost_per_1m_tokens": "1.0000",
                "cost_per_1m_input": "1.0000",
                "cost_per_1m_output": None,
            },
            {
                "model": "unmapped",
                "billed_cost": "0.5000000000",
                "estimated_cost": Z,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_per_1m_tokens": None,
                "cost_per_1m_input": None,
                "cost_per_1m_output": None,
            },
        ],
    }


async def test_summary_filtered(client, seeded):
    resp = await client.get("/api/v1/summary", params=RANGE | {"account_id": "sub-b"})
    assert resp.json() == {
        "currency": "USD",
        "total_cost": "3.5000000000",
        "today_estimated_cost": Z,
        "month_to_date_cost": "3.5000000000",
        "by_model": [
            {
                "model": "gpt-4o",
                "billed_cost": "2.0000000000",
                "estimated_cost": "3.7000000000",
                "input_tokens": 1_500_000,
                "output_tokens": 0,
                "cost_per_1m_tokens": "2.3333",
                "cost_per_1m_input": "2.3333",
                "cost_per_1m_output": None,
            }
        ],
    }
    resp = await client.get("/api/v1/summary", params=RANGE | {"tag": "team:ads"})
    body = resp.json()
    assert (body["total_cost"], body["today_estimated_cost"]) == ("2.0000000000", Z)
    assert [m["model"] for m in body["by_model"]] == ["gpt-4o"]
    # 10-01's 10 is split evenly (no tokens that day) between chat and chat-2.
    assert body["month_to_date_cost"] == "7.0000000000"


async def test_deployments_list(client, seeded):
    body = (await client.get("/api/v1/deployments")).json()
    chat = next(d for d in body if d["name"] == "chat")
    assert [(d["account_name"], d["name"]) for d in body] == [
        ("Prod", "chat"),
        ("Prod", "chat-2"),
        ("Prod", "mini"),
        ("Dev", "dev"),
    ]
    assert chat == {
        "id": seeded["chat"],
        "cloud": "azure",
        "account_id": "sub-a",
        "account_name": "Prod",
        "resource_id": PROD,
        "name": "chat",
        "model": "gpt-4o",
        "model_version": None,
        "sku": None,
        "capacity": None,
        "region": "eastus2",
        "tags": {"team": "search"},
    }


def _drow(day, billed, estimated, inp, out, per_1m, status) -> dict:
    return {
        "day": f"2026-10-{day}",
        "billed_cost": billed,
        "estimated_cost": estimated,
        "input_tokens": inp,
        "output_tokens": out,
        "cost_per_1m_tokens": per_1m,
        "day_status": status,
    }


async def test_deployment_daily(client, seeded):
    resp = await client.get(f"/api/v1/deployments/{seeded['chat']}/daily", params=RANGE)
    body = resp.json()
    assert body["deployment"]["id"] == seeded["chat"]
    assert body["deployment"]["name"] == "chat"
    assert body["rows"] == [
        _drow("13", "8.0000000000", "9.0000000000", 300_000, 100_000, "20.0000", "finalised"),
        _drow("14", "4.0000000000", "3.9000000000", 1_000_000, 0, "4.0000", "billed"),
        _drow("15", Z, "0.7000000000", 200_000, 0, "3.5000", "estimated"),
    ]
    resp = await client.get(f"/api/v1/deployments/{seeded['dev']}/daily", params=RANGE)
    assert resp.json()["rows"] == [
        _drow("13", "2.0000000000", "2.2000000000", 1_000_000, 0, "2.0000", "billed"),
        _drow("14", Z, "1.5000000000", 500_000, 0, "3.0000", "estimated"),
    ]


async def test_deployment_daily_not_found(client, seeded):
    # Another tenant's deployment is as invisible as a random id.
    for dep_id in (seeded["other"], str(uuid.uuid4())):
        resp = await client.get(f"/api/v1/deployments/{dep_id}/daily", params=RANGE)
        assert resp.status_code == 404
    assert (await client.get("/api/v1/deployments/nope/daily")).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "region"},
        {"group_by": "tag:"},
        {"tag": "no-colon"},
        {"tag": ":value"},
        {"start": "2026-10-15", "end": "2026-10-13"},
        {"start": "2025-01-01", "end": "2026-02-05"},
    ],
)
async def test_daily_validation(client, params):
    assert (await client.get("/api/v1/costs/daily", params=params)).status_code == 422


async def test_deployment_daily_range_validation(client, seeded):
    params = {"start": "2026-10-15", "end": "2026-10-13"}
    resp = await client.get(f"/api/v1/deployments/{seeded['chat']}/daily", params=params)
    assert resp.status_code == 422


async def test_openapi_documents_endpoints(app):
    paths = app.openapi()["paths"]
    params = {p["name"] for p in paths["/api/v1/costs/daily"]["get"]["parameters"]}
    assert {"start", "end", "group_by", "account_id", "model", "tag"} <= params
    assert "/api/v1/deployments/{deployment_id}/daily" in paths
    assert {p["name"] for p in paths["/api/v1/summary"]["get"]["parameters"]} >= {"tag", "model"}


def test_ptu_capacity_cost_goes_to_provisioned_deployments():
    from types import SimpleNamespace

    from ai_cost_tracker.api.queries import Inventory

    rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a"
    rows = [
        SimpleNamespace(
            resource_id=rid,
            name="chat",
            model="gpt-4o",
            sku="GlobalStandard",
            tags={},
            deleted_at=None,
        ),
        SimpleNamespace(
            resource_id=rid,
            name="ptu",
            model="gpt-4o",
            sku="ProvisionedManaged",
            tags={},
            deleted_at=None,
        ),
    ]
    inv = Inventory(rows)
    assert [r.name for r in inv.candidates(rid, "provisioned-throughput")] == ["ptu"]
    assert [r.name for r in inv.candidates(rid, "gpt-4o")] == ["chat", "ptu"]
