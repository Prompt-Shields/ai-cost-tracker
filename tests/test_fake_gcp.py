"""FakeGcpConnector and its demo prices: the multi-cloud demo relies on these properties."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ai_cost_tracker.connectors.fake_gcp import FakeGcpConnector
from ai_cost_tracker.connectors.gcp.ids import resource_id
from ai_cost_tracker.db.models import DailyEstimate, Price
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.ingest import ingest_cost, ingest_usage, sync_inventory
from ai_cost_tracker.normalise import parse_meter
from ai_cost_tracker.pricing.demo_gcp import (
    DEMO_EUR_PER_USD,
    demo_gcp_prices,
    seed_gcp_demo_prices,
)

PROJECT = "demo-gcp-project"
RID = resource_id(PROJECT, "us-central1")
WEEK = (date(2025, 3, 3), date(2025, 3, 9))  # Monday..Sunday
SKUS = {
    "Gemini 2.5 Flash Text Input - Predictions": ("gemini-2.5-flash", "input"),
    "Gemini 2.5 Flash Text Output - Predictions": ("gemini-2.5-flash", "output"),
    "Gemini 2.5 Pro Text Input - Predictions": ("gemini-2.5-pro", "input"),
    "Gemini 2.5 Pro Text Output - Predictions": ("gemini-2.5-pro", "output"),
    "Gemini 2.0 Flash Text Input - Predictions": ("gemini-2.0-flash", "input"),
    "Gemini 2.0 Flash Text Output - Predictions": ("gemini-2.0-flash", "output"),
}
USD_PER_1M = {
    "Gemini 2.5 Flash Text Input - Predictions": Decimal("0.30"),
    "Gemini 2.5 Flash Text Output - Predictions": Decimal("2.50"),
    "Gemini 2.5 Pro Text Input - Predictions": Decimal("1.25"),
    "Gemini 2.5 Pro Text Output - Predictions": Decimal("10.00"),
    "Gemini 2.0 Flash Text Input - Predictions": Decimal("0.10"),
    "Gemini 2.0 Flash Text Output - Predictions": Decimal("0.40"),
}


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


async def _setup(today: date | None = None):
    fake = FakeGcpConnector(today=today or date(2026, 1, 1))
    return fake, await fake.list_deployments(PROJECT)


async def test_accounts_and_deployments_shape() -> None:
    fake, deployments = await _setup()
    (account,) = await fake.list_accounts()
    assert (account.cloud, account.account_id, account.name, account.has_ai_resources) == (
        "gcp",
        PROJECT,
        "Demo GCP project",
        True,
    )
    assert fake.name == "fake_gcp" and fake.cloud == "gcp"
    assert await fake.check_access(PROJECT)
    by_name = {d.name: d for d in deployments}
    assert set(by_name) == {"gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash-001"}
    assert by_name["gemini-2.0-flash-001"].model == "gemini-2.0-flash"
    assert by_name["gemini-2.0-flash-001"].model_version == "001"
    assert by_name["gemini-2.5-pro"].tags == {"team": "research", "env": "prod"}
    assert by_name["gemini-2.5-flash"].tags == {"team": "search", "env": "prod"}
    assert by_name["gemini-2.0-flash-001"].tags == {"team": "support", "env": "dev"}
    for d in deployments:
        assert (d.cloud, d.resource_id, d.region, d.sku) == ("gcp", RID, "us-central1", None)
    start = at(WEEK[0])
    assert await fake.pull_ptu_utilization(PROJECT, deployments, start, at(WEEK[1])) == []


async def test_deterministic() -> None:
    a, deps = await _setup()
    b = FakeGcpConnector(today=date(2026, 1, 1))
    start, end = at(WEEK[0]), at(WEEK[0]) + timedelta(days=2)
    assert await a.pull_usage(PROJECT, deps, start, end) == await b.pull_usage(
        PROJECT, deps, start, end
    )
    assert await a.pull_cost(PROJECT, *WEEK) == await b.pull_cost(PROJECT, *WEEK)


async def test_usage_hourly_within_range_and_varies() -> None:
    fake, deps = await _setup()
    start = at(WEEK[0]) + timedelta(hours=5, minutes=30)
    end = start + timedelta(hours=30)
    usage = await fake.pull_usage(PROJECT, deps, start, end)
    assert usage
    floor = start.replace(minute=0)
    assert all(floor <= u.bucket_start < end for u in usage)
    assert all(u.bucket_start.minute == 0 and u.cloud == "gcp" for u in usage)
    assert all(u.resource_id == RID for u in usage)
    pro = [u.input_tokens for u in usage if u.deployment == "gemini-2.5-pro"]
    assert len(set(pro)) > 5  # hourly shape, not flat


async def test_weekend_lower_and_pro_is_biggest_cost() -> None:
    fake, _ = await _setup()
    daily: dict[date, Decimal] = defaultdict(Decimal)
    by_model: dict[str, Decimal] = defaultdict(Decimal)
    for rec in await fake.pull_cost(PROJECT, *WEEK):
        daily[rec.charge_period_start] += rec.billed_cost
        by_model[SKUS[rec.sku_meter][0]] += rec.billed_cost
    weekday = [v for d, v in daily.items() if d.weekday() < 5]
    weekend = [v for d, v in daily.items() if d.weekday() >= 5]
    assert max(weekend) < min(weekday)
    assert max(by_model, key=by_model.get) == "gemini-2.5-pro"
    # A minor but visible share next to the Azure demo (~$1.3k/day incl. PTU).
    assert Decimal(50) < min(weekday) < Decimal(400)


async def test_today_is_not_billed() -> None:
    fake = FakeGcpConnector(today=date(2026, 10, 15))
    days = {
        r.charge_period_start
        for r in await fake.pull_cost(PROJECT, date(2026, 10, 13), date(2026, 10, 15))
    }
    assert days == {date(2026, 10, 13), date(2026, 10, 14)}


async def test_cost_is_tokens_times_usd_price_in_eur_exactly() -> None:
    fake, deps = await _setup()
    model_to_dep = {d.model: d.name for d in deps}
    for day in (WEEK[0], WEEK[1]):
        usage = await fake.pull_usage(PROJECT, deps, at(day), at(day) + timedelta(days=1))
        tokens: dict[tuple[str, str], int] = defaultdict(int)
        for u in usage:
            tokens[(u.deployment, "input")] += u.input_tokens
            tokens[(u.deployment, "output")] += u.output_tokens
        cost = await fake.pull_cost(PROJECT, day, day)
        assert {r.sku_meter for r in cost} == set(SKUS)
        for rec in cost:
            model, direction = SKUS[rec.sku_meter]
            n = tokens[(model_to_dep[model], direction)]
            assert rec.billing_currency == "EUR" and rec.service_name == "Vertex AI"
            assert rec.resource_id == RID and rec.charge_period_start == day
            assert rec.consumed_quantity * 1_000_000 == n
            assert rec.billed_cost == n * USD_PER_1M[rec.sku_meter] / 1_000_000 * Decimal("0.92")


async def test_skus_parse_to_deployment_models() -> None:
    _, deps = await _setup()
    models = {d.model for d in deps}
    assert demo_gcp_prices() == USD_PER_1M
    for sku, (model, direction) in SKUS.items():
        info = parse_meter("gcp", sku)
        assert info is not None
        assert (info.model, info.direction, info.deployment_type) == (model, direction, "global")
        assert info.model in models


async def test_seed_prices_idempotent(session) -> None:
    assert await seed_gcp_demo_prices(session) == 6
    assert await seed_gcp_demo_prices(session) == 6
    rows = (await session.scalars(select(Price))).all()
    assert len(rows) == 6
    for p in rows:
        assert (p.cloud, p.region, p.unit, p.currency, p.tokens_per_unit) == (
            "gcp",
            "us-central1",
            "1M",
            "USD",
            1_000_000,
        )
        assert p.valid_from == date(2025, 1, 1)
        assert p.unit_price == USD_PER_1M[p.sku_meter]
        assert (p.ai_model, p.ai_direction) == SKUS[p.sku_meter]
        assert p.ai_deployment_type == "global"


async def test_estimates_reconcile_with_eur_bill(session, tenant_id) -> None:
    await seed_gcp_demo_prices(session)
    first, last = date(2026, 10, 5), date(2026, 10, 7)
    fake = FakeGcpConnector(today=date(2026, 10, 10))
    found = await sync_inventory(session, tenant_id, fake)
    ((account_id, deployments),) = found.items()
    await ingest_usage(
        session, tenant_id, fake, account_id, deployments, at(first), at(last + timedelta(days=1))
    )
    await ingest_cost(session, tenant_id, fake, account_id, first, last)
    assert await compute_estimates(session, tenant_id, first, last) == 9

    estimates = (await session.scalars(select(DailyEstimate))).all()
    assert len(estimates) == 9 and not any(e.price_missing for e in estimates)
    estimated = {(e.model, e.day): e.estimated_cost for e in estimates}
    assert {e.currency for e in estimates} == {"USD"}

    billed: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for rec in await fake.pull_cost(account_id, first, last):
        billed[(SKUS[rec.sku_meter][0], rec.charge_period_start)] += rec.billed_cost
    assert set(billed) == set(estimated)
    for key, eur in billed.items():
        assert estimated[key] * DEMO_EUR_PER_USD == eur
    assert await session.scalar(select(func.count()).select_from(Price)) == 6
