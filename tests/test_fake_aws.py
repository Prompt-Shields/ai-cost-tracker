"""FakeAwsConnector and its demo prices: the three-cloud demo relies on these properties."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ai_cost_tracker.connectors.aws.ids import resource_id
from ai_cost_tracker.connectors.fake_aws import FakeAwsConnector, usage_types
from ai_cost_tracker.db.models import DailyEstimate, Price
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.ingest import ingest_cost, ingest_usage, sync_inventory
from ai_cost_tracker.normalise import parse_meter
from ai_cost_tracker.pricing.demo_aws import demo_aws_prices, seed_aws_demo_prices

ACCOUNT = "123456789012"
RID = resource_id(ACCOUNT, "us-east-1")
WEEK = (date(2025, 3, 3), date(2025, 3, 9))  # Monday..Sunday
SONNET = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
HAIKU = "anthropic.claude-3-5-haiku-20241022-v1:0"
NOVA = "amazon.nova-pro-v1:0"
# Region-stripped usage type -> (canonical model, direction).
SKUS = {
    "Claude3.7Sonnet-input-tokens": ("claude-3-7-sonnet", "input"),
    "Claude3.7Sonnet-output-tokens": ("claude-3-7-sonnet", "output"),
    "Claude3.5Haiku-input-tokens": ("claude-3-5-haiku", "input"),
    "Claude3.5Haiku-output-tokens": ("claude-3-5-haiku", "output"),
    "NovaPro-input-tokens": ("nova-pro", "input"),
    "NovaPro-output-tokens": ("nova-pro", "output"),
}
SKU_DEPLOYMENT = {
    "Claude3.7Sonnet": SONNET,
    "Claude3.5Haiku": HAIKU,
    "NovaPro": NOVA,
}
USD_PER_1M = {
    "Claude3.7Sonnet-input-tokens": Decimal("3.00"),
    "Claude3.7Sonnet-output-tokens": Decimal("15.00"),
    "Claude3.5Haiku-input-tokens": Decimal("0.80"),
    "Claude3.5Haiku-output-tokens": Decimal("4.00"),
    "NovaPro-input-tokens": Decimal("0.80"),
    "NovaPro-output-tokens": Decimal("3.20"),
}


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _split(sku: str) -> tuple[str, str]:
    words, direction, _ = sku.rsplit("-", 2)
    return SKU_DEPLOYMENT[words], direction


async def _setup(today: date | None = None):
    fake = FakeAwsConnector(today=today or date(2026, 1, 1))
    return fake, await fake.list_deployments(ACCOUNT)


async def test_accounts_and_deployments_shape() -> None:
    fake, deployments = await _setup()
    (account,) = await fake.list_accounts()
    assert (account.cloud, account.account_id, account.name, account.has_ai_resources) == (
        "aws",
        ACCOUNT,
        "Demo AWS account",
        True,
    )
    assert await fake.list_accounts(include_all=True) == [account]
    assert fake.name == "fake_aws" and fake.cloud == "aws"
    assert await fake.check_access(ACCOUNT)
    by_name = {d.name: d for d in deployments}
    assert set(by_name) == {SONNET, HAIKU, NOVA}
    assert by_name[SONNET].tags == {"team": "research", "env": "prod"}
    assert by_name[HAIKU].tags == {"team": "support", "env": "prod"}
    assert by_name[NOVA].tags == {"team": "search", "env": "dev"}
    for d in deployments:
        assert (d.cloud, d.account_id, d.resource_id, d.region, d.sku, d.capacity) == (
            "aws",
            ACCOUNT,
            RID,
            "us-east-1",
            None,
            None,
        )
    assert RID == "//bedrock.amazonaws.com/accounts/123456789012/regions/us-east-1"
    start = at(WEEK[0])
    assert await fake.pull_ptu_utilization(ACCOUNT, deployments, start, at(WEEK[1])) == []


async def test_deployment_models_are_canonical() -> None:
    # Needs W5's aws_usage rules (dates and versions dropped); fails on the foundation cut.
    _, deployments = await _setup()
    by_name = {d.name: d.model for d in deployments}
    assert by_name == {SONNET: "claude-3-7-sonnet", HAIKU: "claude-3-5-haiku", NOVA: "nova-pro"}


async def test_deterministic() -> None:
    a, deps = await _setup()
    b = FakeAwsConnector(today=date(2026, 1, 1))
    start, end = at(WEEK[0]), at(WEEK[0]) + timedelta(days=2)
    assert await a.pull_usage(ACCOUNT, deps, start, end) == await b.pull_usage(
        ACCOUNT, deps, start, end
    )
    assert await a.pull_cost(ACCOUNT, *WEEK) == await b.pull_cost(ACCOUNT, *WEEK)


async def test_usage_hourly_within_range_and_varies() -> None:
    fake, deps = await _setup()
    start = at(WEEK[0]) + timedelta(hours=5, minutes=30)
    end = start + timedelta(hours=30)
    usage = await fake.pull_usage(ACCOUNT, deps, start, end)
    assert usage
    floor = start.replace(minute=0)
    assert all(floor <= u.bucket_start < end for u in usage)
    assert all(u.bucket_start.minute == 0 and u.cloud == "aws" for u in usage)
    assert all(u.resource_id == RID and u.account_id == ACCOUNT for u in usage)
    assert {u.deployment for u in usage} == {SONNET, HAIKU, NOVA}
    assert len({u.bucket_start for u in usage}) == 31  # 05:00 floor .. 11:00 next day
    sonnet = [u.input_tokens for u in usage if u.deployment == SONNET]
    assert len(set(sonnet)) > 5  # hourly shape, not flat
    assert all(u.input_tokens > 0 and u.output_tokens > 0 for u in usage)


async def test_usage_ignores_unknown_deployments() -> None:
    fake, deps = await _setup()
    other = deps[0].model_copy(update={"name": "meta.llama3-1-70b-instruct-v1:0"})
    assert await fake.pull_usage(ACCOUNT, [other], at(WEEK[0]), at(WEEK[1])) == []


async def test_weekend_lower_sonnet_biggest_and_believable_share() -> None:
    fake, _ = await _setup()
    daily: dict[date, Decimal] = defaultdict(Decimal)
    by_model: dict[str, Decimal] = defaultdict(Decimal)
    for rec in await fake.pull_cost(ACCOUNT, *WEEK):
        daily[rec.charge_period_start] += rec.billed_cost
        by_model[_split(rec.sku_meter)[0]] += rec.billed_cost
    weekday = [v for d, v in daily.items() if d.weekday() < 5]
    weekend = [v for d, v in daily.items() if d.weekday() >= 5]
    assert max(weekend) < min(weekday)
    assert max(by_model, key=by_model.get) == SONNET
    # Between Azure's token spend (~$60/day) and the GCP demo (~$150/day).
    assert Decimal(40) < min(weekday) and max(weekday) < Decimal(250)


async def test_today_is_not_billed() -> None:
    fake = FakeAwsConnector(today=date(2026, 10, 15))
    days = {
        r.charge_period_start
        for r in await fake.pull_cost(ACCOUNT, date(2026, 10, 13), date(2026, 10, 15))
    }
    assert days == {date(2026, 10, 13), date(2026, 10, 14)}
    assert await fake.pull_cost(ACCOUNT, date(2026, 10, 15), date(2026, 10, 16)) == []


async def test_cost_is_tokens_times_usd_price_exactly() -> None:
    fake, deps = await _setup()
    for day in (WEEK[0], WEEK[1]):
        usage = await fake.pull_usage(ACCOUNT, deps, at(day), at(day) + timedelta(days=1))
        tokens: dict[tuple[str, str], int] = defaultdict(int)
        for u in usage:
            tokens[(u.deployment, "input")] += u.input_tokens
            tokens[(u.deployment, "output")] += u.output_tokens
        cost = await fake.pull_cost(ACCOUNT, day, day)
        assert {r.sku_meter for r in cost} == set(SKUS)
        for rec in cost:
            n = tokens[_split(rec.sku_meter)]
            assert rec.cloud == "aws" and rec.sub_account_id == ACCOUNT
            assert rec.billing_currency == "USD" and rec.service_name == "Amazon Bedrock"
            assert rec.resource_id == RID and rec.charge_period_start == day
            assert rec.consumed_quantity * 1_000_000 == n
            assert rec.billed_cost == n * USD_PER_1M[rec.sku_meter] / 1_000_000


async def test_usage_types_and_prices() -> None:
    assert usage_types() == ["USE1-" + sku for sku in SKUS]
    assert demo_aws_prices() == USD_PER_1M


async def test_usage_types_parse_to_deployment_models() -> None:
    # Needs W5's aws_usage rules; fails on the foundation cut.
    _, deps = await _setup()
    models = {d.model for d in deps}
    for sku, (model, direction) in SKUS.items():
        for spelling in (sku, "USE1-" + sku):
            info = parse_meter("aws", spelling)
            assert info is not None, spelling
            assert (info.model, info.direction, info.deployment_type) == (
                model,
                direction,
                "global",
            )
        assert model in models


async def test_seed_prices_idempotent(session) -> None:
    assert await seed_aws_demo_prices(session) == 6
    assert await seed_aws_demo_prices(session) == 6
    rows = (await session.scalars(select(Price))).all()
    assert len(rows) == 6
    for p in rows:
        assert (p.cloud, p.region, p.unit, p.currency, p.tokens_per_unit) == (
            "aws",
            "us-east-1",
            "1M",
            "USD",
            1_000_000,
        )
        assert p.valid_from == date(2025, 1, 1)
        assert p.unit_price == USD_PER_1M[p.sku_meter]
        info = parse_meter("aws", p.sku_meter)
        assert (p.ai_model, p.ai_direction) == (
            (info.model, info.direction) if info else (None,) * 2
        )


async def test_seed_prices_metadata(session) -> None:
    # Needs W5's aws_usage rules; fails on the foundation cut.
    await seed_aws_demo_prices(session)
    for p in (await session.scalars(select(Price))).all():
        assert (p.ai_model, p.ai_direction) == SKUS[p.sku_meter]
        assert p.ai_deployment_type == "global"


async def test_estimates_reconcile_with_usd_bill(session, tenant_id) -> None:
    # Needs W5's aws_usage rules (usage types and ModelIds share a canonical model).
    await seed_aws_demo_prices(session)
    first, last = date(2026, 10, 5), date(2026, 10, 7)
    fake = FakeAwsConnector(today=date(2026, 10, 10))
    found = await sync_inventory(session, tenant_id, fake)
    ((account_id, deployments),) = found.items()
    await ingest_usage(
        session, tenant_id, fake, account_id, deployments, at(first), at(last + timedelta(days=1))
    )
    await ingest_cost(session, tenant_id, fake, account_id, first, last)
    assert await compute_estimates(session, tenant_id, first, last) == 9

    estimates = (await session.scalars(select(DailyEstimate))).all()
    assert len(estimates) == 9 and not any(e.price_missing for e in estimates)
    assert {e.currency for e in estimates} == {"USD"}
    estimated = {(e.model, e.day): e.estimated_cost for e in estimates}

    billed: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for rec in await fake.pull_cost(account_id, first, last):
        billed[(SKUS[rec.sku_meter][0], rec.charge_period_start)] += rec.billed_cost
    assert set(billed) == set(estimated)
    for key, usd in billed.items():
        assert estimated[key] == usd
    assert await session.scalar(select(func.count()).select_from(Price)) == 6
