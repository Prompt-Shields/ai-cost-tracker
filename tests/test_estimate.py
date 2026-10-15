import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select

from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.db.models import DailyEstimate, DeploymentRow, Price, TokenUsageRow
from ai_cost_tracker.estimate import compute_estimates, deployment_type, normalise_model
from ai_cost_tracker.pricing.retail import sync_prices

FIXTURES = Path(__file__).parent / "fixtures" / "prices"
START, END = date(2025, 3, 3), date(2025, 3, 9)


async def seed_prices(session) -> None:
    pages = [json.loads((FIXTURES / f"retail_page{i}.json").read_text()) for i in (1, 2)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[1 if "$skip" in str(request.url) else 0])

    await sync_prices(session, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def seed_fake(session, tenant_id, start: date = START, end: date = END):
    """Fake deployments + hourly tokens in the DB; returns the fake cost records."""
    fake = FakeConnector()
    (account,) = await fake.list_accounts()
    deps = await fake.list_deployments(account.account_id)
    for d in deps:
        session.add(
            DeploymentRow(
                tenant_id=tenant_id,
                cloud=d.cloud,
                account_id=d.account_id,
                resource_id=d.resource_id,
                name=d.name,
                model=d.model,
                model_version=d.model_version,
                sku=d.sku,
                region=d.region,
                tags=d.tags,
            )
        )
    lo = datetime.combine(start, datetime.min.time(), UTC)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)
    for u in await fake.pull_usage(account.account_id, deps, lo, hi):
        session.add(
            TokenUsageRow(
                tenant_id=tenant_id,
                cloud=u.cloud,
                account_id=u.account_id,
                resource_id=u.resource_id,
                deployment=u.deployment,
                model=u.model,
                bucket_start=u.bucket_start,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                requests=u.requests,
            )
        )
    await session.commit()
    return await fake.pull_cost(account.account_id, start, end)


def test_normalise_model_matches_meter_names():
    assert normalise_model("gpt-4o") == normalise_model("gpt-4o-2024-08-06") == "gpt-4o"
    assert normalise_model("GPT 4o mini") == "gpt-4o-mini"
    assert normalise_model("gpt-3.5-turbo") == "gpt-35-turbo"
    assert normalise_model(None) is None


def test_deployment_type_mapping():
    assert deployment_type("GlobalStandard") == "global"
    assert deployment_type("DataZoneStandard") == "data_zone"
    assert deployment_type("Standard") == "regional"
    assert deployment_type("GlobalBatch") == "batch"
    assert deployment_type("ProvisionedManaged") == "provisioned"


async def test_estimates_reproduce_fake_billed_cost(session, tenant_id):
    await seed_prices(session)
    costs = await seed_fake(session, tenant_id)
    n = await compute_estimates(session, tenant_id, START, END)
    assert n == 3 * 7

    billed: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for c in costs:
        billed[(c.resource_id, c.charge_period_start)] += c.billed_cost
    rows = list(await session.scalars(select(DailyEstimate)))
    assert len(rows) == n
    for r in rows:
        assert not r.price_missing
        assert r.estimated_cost == r.input_cost + r.output_cost
        expected = billed[(r.resource_id, r.day)]
        # Hourly token counts are truncated to ints, so allow a hair of rounding.
        assert abs(r.estimated_cost - expected) <= expected * Decimal("0.0001"), r.deployment


async def test_estimates_are_idempotent(session, tenant_id):
    await seed_prices(session)
    await seed_fake(session, tenant_id)
    await compute_estimates(session, tenant_id, START, END)
    first = {
        (r.deployment, r.day, r.estimated_cost)
        for r in await session.scalars(select(DailyEstimate))
    }
    await compute_estimates(session, tenant_id, START, END)
    second = {
        (r.deployment, r.day, r.estimated_cost)
        for r in await session.scalars(select(DailyEstimate))
    }
    assert first == second and len(second) == 21


def _price(meter, model, direction, dtype, region, price, valid_from=date(2025, 1, 1)):
    return Price(
        cloud="azure",
        sku_meter=meter,
        region=region,
        unit="1M",
        unit_price=Decimal(price),
        currency="USD",
        valid_from=valid_from,
        ai_model=model,
        ai_direction=direction,
        ai_deployment_type=dtype,
        tokens_per_unit=1_000_000,
    )


async def _one_deployment(session, tenant_id, *, model="gpt-4o", sku="Standard", region="eastus"):
    session.add(
        DeploymentRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id="sub",
            resource_id="r",
            name="d",
            model=model,
            sku=sku,
            region=region,
            tags={},
        )
    )
    session.add(
        TokenUsageRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id="sub",
            resource_id="r",
            deployment="d",
            model=model,
            bucket_start=datetime(2025, 3, 3, 10, tzinfo=UTC),
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
    )
    await session.commit()


async def _estimate(session, tenant_id) -> DailyEstimate:
    await compute_estimates(session, tenant_id, START, START)
    return await session.scalar(select(DailyEstimate))


async def test_region_match_then_fallback(session, tenant_id):
    session.add_all(
        [
            _price("in-e", "gpt-4o", "input", "regional", "eastus", "2.75"),
            _price("in-w", "gpt-4o", "input", "regional", "westeurope", "3.00"),
            _price("out-w", "gpt-4o", "output", "regional", "westeurope", "12"),
        ]
    )
    await _one_deployment(session, tenant_id)
    row = await _estimate(session, tenant_id)
    assert row.input_cost == Decimal("2.75")  # exact region
    assert row.output_cost == Decimal("12")  # any region, same type
    assert not row.price_missing


async def test_falls_back_to_global_and_uses_history(session, tenant_id):
    session.add_all(
        [
            _price("in-g", "gpt-4o", "input", "global", "eastus2", "2.50", date(2024, 1, 1)),
            _price("in-g", "gpt-4o", "input", "global", "eastus2", "2.00", date(2025, 3, 1)),
            _price("in-g", "gpt-4o", "input", "global", "eastus2", "1.00", date(2025, 4, 1)),
            _price("out-g", "gpt-4o", "output", "global", "eastus2", "10"),
        ]
    )
    await _one_deployment(session, tenant_id, sku="DataZoneStandard")
    row = await _estimate(session, tenant_id)
    assert (row.input_cost, row.output_cost) == (Decimal("2"), Decimal("10"))


async def test_price_missing(session, tenant_id):
    session.add(_price("in-g", "gpt-4o", "input", "global", "eastus2", "2.50"))
    await _one_deployment(session, tenant_id, sku="GlobalStandard")
    row = await _estimate(session, tenant_id)
    assert row.price_missing
    assert row.input_cost == Decimal("2.5") and row.output_cost == 0


async def test_ptu_has_no_token_estimate(session, tenant_id):
    session.add(_price("in-p", "gpt-4o", "input", "provisioned", "eastus", "2.50"))
    await _one_deployment(session, tenant_id, sku="ProvisionedManaged")
    row = await _estimate(session, tenant_id)
    assert row.estimated_cost == 0 and not row.price_missing
    assert row.input_tokens == 1_000_000


async def test_audio_prices_not_applied_to_text_tokens(session, tenant_id):
    """Price sync stores audio/image meters as e.g. "audio_input"; estimates use text only."""
    pages = [
        {
            "Items": [
                {
                    "meterName": name,
                    "armRegionName": "eastus2",
                    "unitPrice": price,
                    "unitOfMeasure": "1M",
                    "currencyCode": "USD",
                    "type": "Consumption",
                    "effectiveStartDate": "2025-01-01T00:00:00Z",
                }
                for name, price in [
                    ("gpt-4o-realtime-preview-1217-aud-Inp-glbl Tokens", 40),
                    ("gpt-4o-realtime-preview-1217-txt-Inp-glbl Tokens", 5),
                ]
            ],
            "NextPageLink": None,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[0])

    await sync_prices(session, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    directions = {p.ai_direction for p in await session.scalars(select(Price))}
    assert directions == {"audio_input", "input"}

    await _one_deployment(session, tenant_id, model="gpt-4o-realtime-preview", sku="GlobalStandard")
    row = await _estimate(session, tenant_id)
    assert row.input_cost == Decimal("5")  # text price, not the $40 audio price
