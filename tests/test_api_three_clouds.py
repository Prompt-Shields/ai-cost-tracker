"""Three clouds in one view (M4 exit criterion): Azure (USD), GCP (EUR) and AWS (USD).

Seed (today = 2026-10-15, range 10-13..10-15):

- Azure sub-a, deployment oai-prod/chat (gpt-4o): 10-13 billed 10 USD, 10-14 estimate 4 USD.
- GCP demo-proj us-central1, gemini-2.5-pro: 10-13 billed 8 EUR (= 10 USD), 10-14 estimate 3.
- AWS 123456789012 us-east-1 (rows built directly, ids via ``connectors.aws.ids``):
  on-demand ``us.anthropic.claude-3-7-sonnet-20250219-v1:0`` billed 6 USD on 10-13 (with
  tokens) and estimated 2 USD on 10-14; provisioned throughput ``research-pt`` serving the same
  model billed 20 USD capacity on 10-13 (no tokens).
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
from ai_cost_tracker.api.queries import deployment_label
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors.aws.ids import resource_id
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    FxRate,
    TokenUsageRow,
)
from ai_cost_tracker.db.session import get_session

PRINCIPAL = Principal(oid="u1", name="Ada", email="ada@example.com", roles={"viewer"})
RANGE = {"start": "2026-10-13", "end": "2026-10-15"}
AZ = (
    "/subscriptions/sub-a/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-prod"
)
GCP = "//aiplatform.googleapis.com/projects/demo-proj/locations/us-central1"
ACCOUNT = "123456789012"
AWS = resource_id(ACCOUNT, "us-east-1")
SONNET = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
D13, D14, D15 = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
Z = Decimal("0.0000000000")


def _money(value: str) -> str:
    return format(Decimal(value).quantize(Z), "f")


@pytest.fixture
def app(engine):
    def settings() -> Settings:
        return Settings(environment="test", default_tenant_slug="internal")

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


def _usage(t, cloud, account, resource, deployment, model, inp, out) -> TokenUsageRow:
    return TokenUsageRow(
        tenant_id=t,
        cloud=cloud,
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model=model,
        bucket_start=datetime(2026, 10, 13, 10, tzinfo=UTC),
        input_tokens=inp,
        output_tokens=out,
        requests=1,
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
    session.add_all(
        [
            CloudAccountRow(tenant_id=t, cloud="aws", account_id=ACCOUNT, name="Demo AWS"),
            CloudAccountRow(tenant_id=t, cloud="gcp", account_id="demo-proj", name="Demo GCP"),
            CloudAccountRow(tenant_id=t, cloud="azure", account_id="sub-a", name="Prod"),
            _dep(t, "azure", "sub-a", AZ, "chat", "gpt-4o"),
            _dep(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro"),
            _dep(t, "aws", ACCOUNT, AWS, SONNET, "claude-3-7-sonnet", region="us-east-1"),
            _dep(
                t,
                "aws",
                ACCOUNT,
                AWS,
                "research-pt",
                "claude-3-7-sonnet",
                sku="ProvisionedThroughput",
                capacity=1,
                region="us-east-1",
            ),
            _cost(t, "azure", "sub-a", AZ, D13, "gpt-4o In", "gpt-4o", "input", "10", "USD"),
            _cost(
                t, "gcp", "demo-proj", GCP, D13, "Gemini In", "gemini-2.5-pro", "input", "8", "EUR"
            ),
            _cost(
                t,
                "aws",
                ACCOUNT,
                AWS,
                D13,
                "Claude3.7Sonnet-input-tokens",
                "claude-3-7-sonnet",
                "input",
                "6",
                "USD",
            ),
            _cost(
                t,
                "aws",
                ACCOUNT,
                AWS,
                D13,
                "ProvisionedThroughput-Claude3.7Sonnet-ModelUnits",
                "claude-3-7-sonnet",
                "provisioned",
                "20",
                "USD",
            ),
            _est(t, "azure", "sub-a", AZ, "chat", "gpt-4o", D14, "4"),
            _est(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", D14, "3"),
            _est(t, "aws", ACCOUNT, AWS, SONNET, "claude-3-7-sonnet", D14, "2"),
            _usage(t, "azure", "sub-a", AZ, "chat", "gpt-4o", 1_000, 0),
            _usage(t, "gcp", "demo-proj", GCP, "gemini-2.5-pro", "gemini-2.5-pro", 500, 100),
            _usage(t, "aws", ACCOUNT, AWS, SONNET, "claude-3-7-sonnet", 2_000, 300),
            FxRate(day=D13, base="EUR", quote="USD", rate=Decimal("1.25"), source="demo"),
            FxRate(day=D14, base="EUR", quote="USD", rate=Decimal("1.25"), source="demo"),
        ]
    )
    await session.commit()
    deps = (await session.execute(DeploymentRow.__table__.select())).all()
    return {r.name: str(r.id) for r in deps}


async def _get(client, path: str, **params) -> dict | list:
    resp = await client.get(f"/api/v1{path}", params=RANGE | params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_summary_has_three_clouds_in_reporting_currency(client, seeded):
    body = await _get(client, "/summary")
    assert body["currency"] == "USD"
    assert body["fx_approximate"] is False
    # Azure 10 + 4; GCP 8 EUR = 10 USD + 3; AWS 6 + 20 billed + 2 estimated.
    assert body["total_cost"] == _money("55")
    assert [(c["cloud"], c["total_cost"]) for c in body["by_cloud"]] == [
        ("aws", _money("28")),
        ("azure", _money("14")),
        ("gcp", _money("13")),
    ]
    aws = body["by_cloud"][0]
    assert (aws["billed_cost"], aws["estimated_cost"]) == (_money("26"), _money("2"))
    assert (aws["input_tokens"], aws["output_tokens"]) == (2_000, 300)
    sonnet = next(m for m in body["by_model"] if m["model"] == "claude-3-7-sonnet")
    assert sonnet["billed_cost"] == _money("26")


async def test_clouds_ordered_azure_gcp_aws_with_nouns(client, seeded):
    clouds = await _get(client, "/clouds")
    assert [(c["cloud"], c["label"], c["account_noun"]) for c in clouds] == [
        ("azure", "Azure", "subscription"),
        ("gcp", "Google Cloud", "project"),
        ("aws", "AWS", "account"),
    ]
    aws = clouds[2]
    assert (aws["accounts"], aws["tracked_accounts"], aws["deployments"]) == (1, 1, 2)


async def test_aws_filter_and_group_by_deployment(client, seeded):
    body = await _get(client, "/costs/daily", group_by="deployment", cloud="aws")
    billed = {r["key"]: r["billed_cost"] for r in body["rows"] if r["day"] == "2026-10-13"}
    # The capacity charge goes to the provisioned throughput, not the on-demand profile that
    # serves the same model (and has the tokens).
    assert billed == {
        f"{ACCOUNT}/us-east-1/{SONNET}": _money("6"),
        f"{ACCOUNT}/us-east-1/research-pt": _money("20"),
    }
    accounts = await _get(client, "/costs/daily", group_by="account", cloud="aws")
    assert {r["key"] for r in accounts["rows"]} == {ACCOUNT}
    clouds = await _get(client, "/costs/daily", group_by="cloud")
    assert {r["key"] for r in clouds["rows"]} == {"azure", "gcp", "aws"}


async def test_aws_deployments_and_detail(client, seeded):
    deps = await _get(client, "/deployments", cloud="aws")
    assert [(d["name"], d["account_name"], d["region"], d["sku"]) for d in deps] == [
        ("research-pt", "Demo AWS", "us-east-1", "ProvisionedThroughput"),
        (SONNET, "Demo AWS", "us-east-1", None),
    ]
    detail = await _get(client, f"/deployments/{seeded['research-pt']}/daily")
    assert detail["deployment"]["resource_id"] == AWS
    assert [(r["day"], r["billed_cost"]) for r in detail["rows"]] == [("2026-10-13", _money("20"))]


async def test_usage_export_lists_aws(client, seeded):
    usage = await _get(client, "/usage/daily", cloud="aws")
    assert [(r["cloud"], r["deployment"], r["model"]) for r in usage["rows"]] == [
        ("aws", SONNET, "claude-3-7-sonnet")
    ]


def test_deployment_label_aws():
    assert deployment_label(AWS, SONNET) == f"{ACCOUNT}/us-east-1/{SONNET}"
    assert deployment_label(AWS, None) == f"{ACCOUNT}/us-east-1/unattributed"
    assert deployment_label(resource_id(ACCOUNT, None), "x") == f"{ACCOUNT}/global/x"
