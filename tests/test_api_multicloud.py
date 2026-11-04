"""Multi-cloud overview API (issue #50): cloud filter and grouping, totals in one currency.

Seed (today = 2026-10-15, range 10-13..10-15):

- Azure sub-a (tracked) + sub-z (untracked, no data); deployment oai-prod/chat (gpt-4o).
  10-13 billed 10 USD, 10-14 estimate 4 USD.
- GCP project demo-proj, Vertex AI us-central1, publisher model gemini-2.5-pro (plus a deleted
  one). 10-13 billed 8 EUR (= 10 USD at 1.25), 10-14 estimate 2 USD, 10-15 estimate 1 USD.
- FX: EUR->USD 1.25 on 10-13 and 10-14 (unless a test changes it).
- Another tenant's GCP rows must never show up.
"""

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import get_today
from ai_cost_tracker.api.queries import deployment_label
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    FxRate,
    Tenant,
    TokenUsageRow,
)
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-13", "end": "2026-10-15"}
AZ = (
    "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-prod"
)
GCP = "//aiplatform.googleapis.com/projects/demo-proj/locations/us-central1"
D13, D14, D15 = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
Z = "0.0000000000"


def _money(value: str) -> str:
    return format(Decimal(value).quantize(Decimal(Z)), "f")


@pytest.fixture
def reporting_currency() -> str:
    return "USD"


@pytest.fixture
def app(engine, reporting_currency):
    def settings() -> Settings:
        return Settings(
            environment="test",
            default_tenant_slug="internal",
            reporting_currency=reporting_currency,
        )

    app = create_app(settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_principal] = lambda: PRINCIPAL
    app.dependency_overrides[get_today] = lambda: D15
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _cost(t, cloud, account, resource, day, meter, model, direction, cost, ccy) -> CostLine:
    return CostLine(
        tenant_id=t,
        cloud=cloud,
        sub_account_id=account,
        resource_id=resource,
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency=ccy,
        ai_model=model,
        ai_direction=direction,
    )


def _est(t, cloud, account, resource, deployment, model, day, cost) -> DailyEstimate:
    return DailyEstimate(
        tenant_id=t,
        cloud=cloud,
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model=model,
        day=day,
        input_cost=Decimal(cost),
        output_cost=Decimal(0),
        estimated_cost=Decimal(cost),
        currency="USD",
    )


def _usage(t, cloud, account, resource, deployment, model, day, inp, out) -> TokenUsageRow:
    return TokenUsageRow(
        tenant_id=t,
        cloud=cloud,
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model=model,
        bucket_start=datetime(day.year, day.month, day.day, 10, tzinfo=UTC),
        input_tokens=inp,
        output_tokens=out,
        requests=2,
    )


def _dep(t, cloud, account, resource, name, model, **kw) -> DeploymentRow:
    return DeploymentRow(
        tenant_id=t,
        cloud=cloud,
        account_id=account,
        resource_id=resource,
        name=name,
        model=model,
        tags={},
        **kw,
    )


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, str]:
    t = tenant_id
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    o = other.id
    pro = "Gemini 2.5 Pro Text Input - Predictions"
    session.add_all(
        [
            CloudAccountRow(tenant_id=t, cloud="gcp", account_id="demo-proj", name="Demo GCP"),
            CloudAccountRow(tenant_id=t, cloud="azure", account_id="sub-a", name="Prod"),
            CloudAccountRow(
                tenant_id=t, cloud="azure", account_id="sub-z", name="Idle", tracked=False
            ),
            _dep(t, "azure", "sub-a", AZ, "chat", "gpt-4o"),
            _dep(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro"),
            _dep(
                t,
                "gcp",
                "demo-proj",
                GCP,
                "gemini-1.5-pro",
                "gemini-1.5-pro",
                deleted_at=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            _cost(t, "azure", "sub-a", AZ, D13, "gpt-4o In", "gpt-4o", "input", "10", "USD"),
            _cost(t, "gcp", "demo-proj", GCP, D13, pro, "gemini-2.5-pro", "input", "8", "EUR"),
            _est(t, "azure", "sub-a", AZ, "chat", "gpt-4o", D14, "4"),
            _est(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", D14, "2"),
            _est(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", D15, "1"),
            _usage(t, "azure", "sub-a", AZ, "chat", "gpt-4o", D13, 1_000_000, 0),
            _usage(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", D13, 500, 100),
            FxRate(day=D13, base="EUR", quote="USD", rate=Decimal("1.25"), source="demo"),
            FxRate(day=D14, base="EUR", quote="USD", rate=Decimal("1.25"), source="demo"),
            # Other tenant
            CloudAccountRow(tenant_id=o, cloud="gcp", account_id="demo-proj", name="Theirs"),
            _dep(o, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro"),
            _cost(o, "gcp", "demo-proj", GCP, D13, pro, "gemini-2.5-pro", "input", "999", "EUR"),
            _usage(o, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", D13, 9, 9),
        ]
    )
    await session.commit()
    deps = (await session.execute(DeploymentRow.__table__.select())).all()
    return {r.name: str(r.id) for r in deps if r.tenant_id == t}


async def _get(client, path: str, **params) -> dict | list:
    resp = await client.get(f"/api/v1{path}", params=RANGE | params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _csv(resp: httpx.Response) -> list[dict[str, str]]:
    assert resp.status_code == 200, resp.text
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))


async def test_summary_totals_in_reporting_currency(client, seeded):
    body = await _get(client, "/summary")
    assert body["currency"] == "USD"
    assert body["fx_approximate"] is False
    # Azure 10 billed + 4 est; GCP 8 EUR -> 10 USD billed + 2 + 1 est.
    assert body["total_cost"] == _money("27")
    assert body["today_estimated_cost"] == _money("1")
    assert body["month_to_date_cost"] == _money("27")
    assert body["by_cloud"] == [
        {
            "cloud": "azure",
            "billed_cost": _money("10"),
            "estimated_cost": _money("4"),
            "total_cost": _money("14"),
            "input_tokens": 1_000_000,
            "output_tokens": 0,
        },
        {
            "cloud": "gcp",
            "billed_cost": _money("10"),
            "estimated_cost": _money("3"),
            "total_cost": _money("13"),
            "input_tokens": 500,
            "output_tokens": 100,
        },
    ]
    gemini = next(m for m in body["by_model"] if m["model"] == "gemini-2.5-pro")
    assert gemini["billed_cost"] == _money("10")


async def test_missing_rate_is_flagged_approximate(client, seeded, session):
    await session.execute(delete(FxRate))
    await session.commit()
    body = await _get(client, "/summary")
    assert body["fx_approximate"] is True
    # No rate at all: the EUR amount is left as it is (and flagged).
    gcp = next(c for c in body["by_cloud"] if c["cloud"] == "gcp")
    assert gcp["billed_cost"] == _money("8")
    daily = await _get(client, "/costs/daily", cloud="gcp")
    assert daily["fx_approximate"] is True
    # Azure alone needs no conversion.
    assert (await _get(client, "/summary", cloud="azure"))["fx_approximate"] is False


async def test_stale_rate_is_flagged_approximate(client, seeded, session):
    await session.execute(delete(FxRate))
    session.add(FxRate(day=date(2026, 10, 1), base="EUR", quote="USD", rate=Decimal("1.25")))
    await session.commit()
    body = await _get(client, "/summary")
    assert body["fx_approximate"] is True
    assert body["total_cost"] == _money("27")  # still converted, just with a 12-day-old rate


@pytest.mark.parametrize("reporting_currency", ["EUR"])
async def test_reporting_currency_setting(client, seeded):
    body = await _get(client, "/summary")
    assert body["currency"] == "EUR"
    assert body["fx_approximate"] is False
    totals = {c["cloud"]: c for c in body["by_cloud"]}
    # Azure: 10 USD billed -> 8 EUR, estimate 4 USD -> 3.2 EUR; GCP 8 EUR billed as is.
    assert totals["azure"]["total_cost"] == _money("11.2")
    assert totals["gcp"]["billed_cost"] == _money("8")


async def test_costs_daily_group_by_cloud(client, seeded):
    body = await _get(client, "/costs/daily", group_by="cloud")
    assert body["currency"] == "USD" and body["fx_approximate"] is False
    got = [(r["day"], r["key"], r["billed_cost"], r["estimated_cost"]) for r in body["rows"]]
    assert got == [
        ("2026-10-13", "azure", _money("10"), Z),
        ("2026-10-13", "gcp", _money("10"), Z),
        ("2026-10-14", "azure", Z, _money("4")),
        ("2026-10-14", "gcp", Z, _money("2")),
        ("2026-10-15", "gcp", Z, _money("1")),
    ]
    assert [r["day_status"] for r in body["rows"]][:2] == ["billed", "billed"]


async def test_cloud_filter_on_cost_endpoints(client, seeded):
    gcp = await _get(client, "/costs/daily", cloud="gcp")
    assert {r["key"] for r in gcp["rows"]} == {"gemini-2.5-pro"}
    azure = await _get(client, "/costs/daily", cloud="azure")
    assert {r["key"] for r in azure["rows"]} == {"gpt-4o"}

    summary = await _get(client, "/summary", cloud="azure")
    assert summary["total_cost"] == _money("14")
    assert [c["cloud"] for c in summary["by_cloud"]] == ["azure"]
    assert [m["model"] for m in summary["by_model"]] == ["gpt-4o"]

    usage = await _get(client, "/usage/daily", cloud="gcp")
    assert usage["rows"] == [
        {
            "day": "2026-10-13",
            "cloud": "gcp",
            "deployment": "gemini-2.5-pro",
            "model": "gemini-2.5-pro",
            "input_tokens": 500,
            "output_tokens": 100,
            "requests": 2,
        }
    ]
    all_usage = await _get(client, "/usage/daily")
    assert {(r["cloud"], r["deployment"]) for r in all_usage["rows"]} == {
        ("azure", "chat"),
        ("gcp", "gemini-2.5-pro"),
    }


async def test_cloud_filter_rejects_unknown_cloud(client, seeded):
    for path in ("/costs/daily", "/summary", "/usage/daily", "/deployments", "/accounts"):
        resp = await client.get(f"/api/v1{path}", params={"cloud": "oracle"})
        assert resp.status_code == 422, path


async def test_cloud_filter_on_inventory(client, seeded):
    deps = await _get(client, "/deployments", cloud="gcp")
    assert [(d["cloud"], d["name"]) for d in deps] == [("gcp", "gemini-2.5-pro")]
    assert len(await _get(client, "/deployments")) == 2
    accounts = await _get(client, "/accounts", cloud="gcp")
    assert [(a["cloud"], a["account_id"]) for a in accounts] == [("gcp", "demo-proj")]
    assert {a["cloud"] for a in await _get(client, "/accounts", cloud="azure")} == {"azure"}


async def test_clouds(client, seeded):
    assert await _get(client, "/clouds") == [
        {
            "cloud": "azure",
            "label": "Azure",
            "accounts": 2,
            "tracked_accounts": 1,
            "deployments": 1,
        },
        {
            "cloud": "gcp",
            "label": "Google Cloud",
            "accounts": 1,
            "tracked_accounts": 1,
            "deployments": 1,
        },
    ]


async def test_clouds_empty_without_accounts(client, tenant_id):
    assert await _get(client, "/clouds") == []


def test_deployment_label():
    assert deployment_label(GCP, "gemini-2.5-pro") == "demo-proj/us-central1/gemini-2.5-pro"
    assert deployment_label(GCP, None) == "demo-proj/us-central1/unattributed"
    assert deployment_label(AZ, "chat") == "oai-prod/chat"


async def test_group_by_deployment_uses_gcp_label(client, seeded):
    body = await _get(client, "/costs/daily", group_by="deployment")
    assert {r["key"] for r in body["rows"]} == {
        "oai-prod/chat",
        "demo-proj/us-central1/gemini-2.5-pro",
    }


async def test_deployment_daily_converts(client, seeded):
    body = await _get(client, f"/deployments/{seeded['gemini-2.5-pro']}/daily")
    assert body["currency"] == "USD" and body["fx_approximate"] is False
    assert body["deployment"]["cloud"] == "gcp"
    rows = {r["day"]: r for r in body["rows"]}
    assert rows["2026-10-13"]["billed_cost"] == _money("10")
    assert rows["2026-10-14"]["estimated_cost"] == _money("2")


async def test_csv_exports_honour_cloud(client, seeded):
    p = RANGE | {"cloud": "gcp"}
    rows = _csv(await client.get("/api/v1/exports/costs-daily.csv", params=p))
    assert {r["key"] for r in rows} == {"gemini-2.5-pro"}
    assert {r["currency"] for r in rows} == {"USD"}

    by_cloud = _csv(
        await client.get("/api/v1/exports/costs-daily.csv", params=RANGE | {"group_by": "cloud"})
    )
    assert {r["key"] for r in by_cloud} == {"azure", "gcp"}

    usage = _csv(await client.get("/api/v1/exports/usage-daily.csv", params=p))
    assert [(r["cloud"], r["deployment"]) for r in usage] == [("gcp", "gemini-2.5-pro")]

    deps = _csv(await client.get("/api/v1/exports/deployments.csv", params={"cloud": "gcp"}))
    assert [r["name"] for r in deps] == ["gemini-2.5-pro"]

    summary = _csv(await client.get("/api/v1/exports/summary.csv", params=p))
    assert [r["model"] for r in summary] == ["gemini-2.5-pro"]
    assert {r["total_cost"] for r in summary} == {_money("13")}
