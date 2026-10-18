"""FakeConnector specifics the demo dashboard relies on (beyond the generic contract)."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ai_cost_tracker.connectors.base import is_provisioned
from ai_cost_tracker.connectors.fake import PTU_METER, FakeConnector

# List prices the fake bills at, $ per 1M tokens: (input, output).
PRICES = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "o3-mini": (Decimal("1.10"), Decimal("4.40")),
}
WEEK = (date(2025, 3, 3), date(2025, 3, 9))  # Monday..Sunday


async def _setup():
    fake = FakeConnector()
    account = (await fake.list_accounts())[0].account_id
    return fake, account, await fake.list_deployments(account)


async def test_weekend_cost_lower_than_weekday() -> None:
    fake, account, _ = await _setup()
    daily: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for rec in await fake.pull_cost(account, *WEEK):
        if rec.sku_meter == PTU_METER:  # reserved capacity: same cost every day by design
            continue
        daily[(rec.resource_id, rec.charge_period_start)] += rec.billed_cost
    for resource in {r for r, _ in daily}:
        weekday = [v for (r, d), v in daily.items() if r == resource and d.weekday() < 5]
        weekend = [v for (r, d), v in daily.items() if r == resource and d.weekday() >= 5]
        assert len(weekday) == 5 and len(weekend) == 2
        assert max(weekend) < min(weekday)


async def test_cost_matches_tokens_times_price() -> None:
    fake, account, deployments = await _setup()
    day = WEEK[0]
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    usage = await fake.pull_usage(account, deployments, start, start + timedelta(days=1))
    tokens: dict[tuple[str, str], int] = defaultdict(int)
    model_of = {}
    for row in usage:
        tokens[(row.resource_id, "input")] += row.input_tokens
        tokens[(row.resource_id, "output")] += row.output_tokens
        model_of[row.resource_id] = row.model

    cost = [r for r in await fake.pull_cost(account, day, day) if r.sku_meter != PTU_METER]
    token_deployments = [d for d in deployments if not is_provisioned(d.sku)]
    assert len(cost) == 2 * len(token_deployments)
    for rec in cost:
        direction = "input" if " Inp " in rec.sku_meter else "output"
        price = PRICES[model_of[rec.resource_id]][0 if direction == "input" else 1]
        expected_tokens = tokens[(rec.resource_id, direction)]
        # Usage truncates each hour to an int, cost doesn't: allow up to 1 token/hour drift.
        assert abs(rec.consumed_quantity * 1_000_000 - expected_tokens) <= 24
        expected_cost = Decimal(expected_tokens) * price / 1_000_000
        assert abs(rec.billed_cost - expected_cost) <= Decimal(24) * price / 1_000_000


async def test_today_is_not_billed_yet():
    from datetime import date

    from ai_cost_tracker.connectors.fake import FakeConnector

    fake = FakeConnector(today=date(2026, 10, 15))
    days = {
        r.charge_period_start
        for r in await fake.pull_cost("sub", date(2026, 10, 13), date(2026, 10, 15))
    }
    assert days == {date(2026, 10, 13), date(2026, 10, 14)}
