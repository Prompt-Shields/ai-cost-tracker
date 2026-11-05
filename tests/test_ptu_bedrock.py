"""Bedrock provisioned throughput in the PTU report: cost, but no utilisation from AWS."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker import ptu
from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors.aws.ids import resource_id
from ai_cost_tracker.db.models import CostLine, DeploymentRow, PtuUtilization
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-12", "end": "2026-10-12"}
DAY = date(2026, 10, 12)
ACCOUNT = "123456789012"
AWS = resource_id(ACCOUNT, "us-east-1")
AZ = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-ptu"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
async def client(engine):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: PRINCIPAL
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _pt(t, name: str, model: str, capacity: int) -> DeploymentRow:
    return DeploymentRow(
        tenant_id=t,
        cloud="aws",
        account_id=ACCOUNT,
        resource_id=AWS,
        name=name,
        model=model,
        sku="ProvisionedThroughput",
        capacity=capacity,
        region="us-east-1",
    )


def _line(t, meter: str, model: str | None, cost: str, direction="provisioned") -> CostLine:
    return CostLine(
        tenant_id=t,
        cloud="aws",
        sub_account_id=ACCOUNT,
        resource_id=AWS,
        charge_period_start=DAY,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
        ai_model=model,
        ai_direction=direction,
    )


@pytest.fixture
async def seeded(session, tenant_id):
    t = tenant_id
    session.add_all(
        [
            _pt(t, "sonnet-pt", "claude-3-7-sonnet", 1),
            _pt(t, "haiku-pt", "claude-3-5-haiku", 3),
            # On-demand use of the same model in the same region: never PTU cost.
            DeploymentRow(
                tenant_id=t,
                cloud="aws",
                account_id=ACCOUNT,
                resource_id=AWS,
                name="anthropic.claude-3-5-haiku-20241022-v1:0",
                model="claude-3-5-haiku",
                region="us-east-1",
            ),
            _line(t, "ProvisionedThroughput-Claude3.7Sonnet", "claude-3-7-sonnet", "100"),
            _line(t, "ProvisionedThroughput-Claude3.5Haiku", "claude-3-5-haiku", "30"),
            _line(t, "Claude3.5Haiku-input-tokens", "claude-3-5-haiku", "7", "input"),
            # Azure PTU next to it keeps its utilisation maths.
            DeploymentRow(
                tenant_id=t,
                cloud="azure",
                account_id="s",
                resource_id=AZ,
                name="batch-ptu",
                model="gpt-4o",
                sku="ProvisionedManaged",
                capacity=50,
            ),
            CostLine(
                tenant_id=t,
                cloud="azure",
                sub_account_id="s",
                resource_id=AZ,
                charge_period_start=DAY,
                sku_meter="Provisioned Managed Regional Unit",
                billed_cost=Decimal("1200"),
                billing_currency="USD",
            ),
            PtuUtilization(
                tenant_id=t,
                cloud="azure",
                account_id="s",
                resource_id=AZ,
                deployment="batch-ptu",
                bucket_start=datetime(2026, 10, 12, 9, tzinfo=UTC),
                utilization=Decimal("0.5"),
            ),
        ]
    )
    await session.commit()


async def test_report_marks_aws_utilisation_unreported(session, tenant_id, seeded):
    report = await ptu.ptu_report(session, tenant_id, DAY, DAY)
    by_name = {d.deployment: d for d in report.deployments}
    assert set(by_name) == {"sonnet-pt", "haiku-pt", "batch-ptu"}

    sonnet, haiku = by_name["sonnet-pt"], by_name["haiku-pt"]
    # Each capacity charge names its model, so it is not split across the region by capacity.
    assert sonnet.ptu_cost == Decimal("100")
    assert haiku.ptu_cost == Decimal("30")
    for d in (sonnet, haiku):
        assert d.cloud == "aws"
        assert d.utilization_reported is False
        assert d.utilization_note == "Utilisation not reported by AWS"
        assert d.avg_utilization is None and d.peak_utilization is None
        assert d.idle_cost is None
        assert (d.idle_hours, d.hours) == (0, 0)

    azure = by_name["batch-ptu"]
    assert azure.utilization_reported is True and azure.utilization_note is None
    assert azure.avg_utilization == Decimal("0.5000")
    assert azure.idle_cost == Decimal("600.0000000000")


async def test_unnamed_capacity_charge_splits_by_capacity(session, tenant_id):
    session.add_all(
        [
            _pt(tenant_id, "a-pt", "claude-3-7-sonnet", 1),
            _pt(tenant_id, "b-pt", "claude-3-5-haiku", 3),
            _line(tenant_id, "ProvisionedThroughput-ModelUnits", None, "40"),
        ]
    )
    await session.commit()
    report = await ptu.ptu_report(session, tenant_id, DAY, DAY)
    assert {d.deployment: d.ptu_cost for d in report.deployments} == {
        "a-pt": Decimal("10.0000000000"),
        "b-pt": Decimal("30.0000000000"),
    }


async def test_api_lists_aws_with_unique_label(client, seeded):
    r = await client.get("/api/v1/ptu", params=RANGE)
    assert r.status_code == 200
    rows = {d["deployment"]: d for d in r.json()["deployments"]}
    sonnet = rows["sonnet-pt"]
    assert sonnet["cloud"] == "aws"
    assert sonnet["deployment_key"] == "us-east-1/sonnet-pt"
    assert sonnet["deployment_label"] == f"{ACCOUNT}/us-east-1/sonnet-pt"
    assert sonnet["utilization_reported"] is False
    assert sonnet["utilization_note"] == "Utilisation not reported by AWS"
    assert sonnet["avg_utilization"] is None
    assert sonnet["idle_cost"] is None
    assert sonnet["ptu_cost"] == "100.0000000000"
    assert rows["batch-ptu"]["deployment_label"] == "oai-ptu/batch-ptu"


async def test_hourly_accepts_label_and_legacy_key(client, seeded):
    for key in (f"{ACCOUNT}/us-east-1/sonnet-pt", f"{ACCOUNT}%2Fus-east-1%2Fsonnet-pt"):
        r = await client.get(f"/api/v1/ptu/{key}/hourly", params=RANGE)
        assert r.status_code == 200, key
        assert r.json()["rows"] == []
    legacy = await client.get("/api/v1/ptu/us-east-1/haiku-pt/hourly", params=RANGE)
    assert legacy.status_code == 200
    assert legacy.json()["deployment"] == "haiku-pt"
