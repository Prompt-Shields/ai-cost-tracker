"""Budget spend, forecast, status and the alert job (issue #29).

Seed (today = Thu 2026-10-15): deployment chat (sub-a, gpt-4o, team=search) estimates 10/day
and dev (sub-b, gpt-4o-mini, team=ads) 5/day from 09-20 to 10-15. sub-a has a billed cost
line of 20 on 10-02, which replaces that day's 10 estimate. Another tenant's rows must not
count. MTD: chat 160, dev 75, all 235.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import budgets
from ai_cost_tracker.budgets import budget_status, evaluate, forecast, run_budgets
from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import (
    AlertEvent,
    Budget,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    Tenant,
)

TODAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
_ARM = "resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts"
PROD = f"/subscriptions/sub-a/{_ARM}/oai-prod"
DEV = f"/subscriptions/sub-b/{_ARM}/oai-dev"
SETTINGS = Settings(environment="test", default_tenant_slug="internal")


def _est(t, account, resource, deployment, model, day, cost) -> DailyEstimate:
    return DailyEstimate(
        tenant_id=t,
        cloud="azure",
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


def _dep(t, account, resource, name, model, tags) -> DeploymentRow:
    return DeploymentRow(
        tenant_id=t,
        cloud="azure",
        account_id=account,
        resource_id=resource,
        name=name,
        model=model,
        tags=tags,
    )


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, DeploymentRow]:
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    chat = _dep(tenant_id, "sub-a", PROD, "chat", "gpt-4o", {"team": "search"})
    dev = _dep(tenant_id, "sub-b", DEV, "dev", "gpt-4o-mini", {"team": "ads"})
    session.add_all([chat, dev, _dep(other.id, "sub-a", PROD, "chat", "gpt-4o", {})])
    day = date(2026, 9, 20)
    while day <= TODAY:
        session.add_all(
            [
                _est(tenant_id, "sub-a", PROD, "chat", "gpt-4o", day, 10),
                _est(tenant_id, "sub-b", DEV, "dev", "gpt-4o-mini", day, 5),
                _est(other.id, "sub-a", PROD, "chat", "gpt-4o", day, 1000),
            ]
        )
        day += timedelta(days=1)
    session.add(
        CostLine(
            tenant_id=tenant_id,
            cloud="azure",
            sub_account_id="sub-a",
            resource_id=PROD,
            charge_period_start=date(2026, 10, 2),
            sku_meter="gpt-4o Inp regnl Tokens",
            billed_cost=Decimal(20),
            billing_currency="USD",
            ai_model="gpt-4o",
            ai_direction="input",
        )
    )
    await session.commit()
    return {"chat": chat, "dev": dev}


def _budget(tenant_id, scope_type="all", scope_value=None, amount="1000", **kw) -> Budget:
    return Budget(
        tenant_id=tenant_id,
        name=kw.pop("name", f"{scope_type} budget"),
        scope_type=scope_type,
        scope_value=scope_value,
        amount=Decimal(amount),
        currency="USD",
        thresholds=kw.pop("thresholds", [0.8, 1.0]),
        active=kw.pop("active", True),
    )


@pytest.mark.parametrize(
    ("scope_type", "scope_value", "expected"),
    [
        ("all", None, "235"),
        ("account", "sub-a", "160"),
        ("account", "sub-z", "0"),
        ("model", "gpt-4o-mini", "75"),
        ("deployment", "dev", "75"),
        ("deployment", str(uuid.uuid4()), "0"),
        ("tag", "team:search", "160"),
        ("tag", "team:ads", "75"),
        ("tag", "team:none", "0"),
    ],
)
async def test_month_to_date_per_scope(
    session, tenant_id, seeded, scope_type, scope_value, expected
):
    if scope_type == "deployment" and scope_value in seeded:
        scope_value = str(seeded[scope_value].id)
    ev = await evaluate(session, tenant_id, _budget(tenant_id, scope_type, scope_value), TODAY)
    assert ev.month_to_date == Decimal(expected)


async def test_evaluate_forecast_and_percent(session, tenant_id, seeded):
    ev = await evaluate(session, tenant_id, _budget(tenant_id, amount="470"), TODAY)
    # Flat 15/day every day of the week (history 10-08..10-14): 235 + 16 days x 15 = 475.
    assert ev.forecast_month_end == Decimal(475)
    assert ev.method == "weekday_weekend"
    assert ev.percent_used == 50.0
    assert ev.status == "warning"


def _month_spend(weekday: int, weekend: int, until: date) -> dict[date, Decimal]:
    out, d = {}, date(2026, 9, 20)
    while d <= until:
        out[d] = Decimal(weekend if d.weekday() >= 5 else weekday)
        d += timedelta(days=1)
    return out


def _remaining(start: date, end: date, weekday: int, weekend: int) -> Decimal:
    total, d = Decimal(0), start
    while d <= end:
        total += weekend if d.weekday() >= 5 else weekday
        d += timedelta(days=1)
    return total


def test_forecast_weights_weekdays_and_weekends():
    totals = _month_spend(10, 2, TODAY - timedelta(days=1))
    totals[TODAY] = Decimal(3)  # partial day
    mtd, projected, method = forecast(totals, set(totals), TODAY)
    assert method == "weekday_weekend"
    assert mtd == sum(v for d, v in totals.items() if d.month == 10)
    # Today counts as its expected 10, not the partial 3; then 16 more days.
    assert projected == mtd - 3 + 10 + _remaining(date(2026, 10, 16), date(2026, 10, 31), 10, 2)


def test_forecast_uses_today_when_above_expected():
    totals = _month_spend(10, 10, TODAY)
    totals[TODAY] = Decimal(50)
    mtd, projected, _ = forecast(totals, set(totals), TODAY)
    assert projected == mtd + 16 * 10


def test_forecast_ignores_days_before_data_starts_and_falls_back_to_flat():
    # Data only from Tue 10-13: two weekdays, no weekend -> flat average of 12.
    totals = {date(2026, 10, 13): Decimal(8), date(2026, 10, 14): Decimal(16)}
    mtd, projected, method = forecast(totals, set(totals), TODAY)
    assert method == "flat"
    assert mtd == 24
    assert projected == 24 + 12 + 16 * 12


def test_forecast_without_history_is_mtd():
    totals = {TODAY: Decimal(7)}
    assert forecast(totals, {TODAY}, TODAY) == (Decimal(7), Decimal(7), "none")


def test_forecast_on_first_of_month_uses_last_month():
    first = date(2026, 11, 1)
    totals = _month_spend(10, 10, date(2026, 10, 31))
    mtd, projected, _ = forecast(totals, set(totals), first)
    assert mtd == 0
    assert projected == 30 * 10


@pytest.mark.parametrize(
    ("mtd", "projected", "status"),
    [("100", "500", "ok"), ("100", "800", "warning"), ("800", "800", "warning"),
     ("999", "2000", "warning"), ("1000", "1000", "over"), ("1500", "1500", "over")],
)  # fmt: skip
def test_status(mtd, projected, status):
    b = Budget(amount=Decimal(1000), thresholds=[1.0, 0.8])
    assert budget_status(b, Decimal(mtd), Decimal(projected)) == status


# --- run_budgets -------------------------------------------------------------------------


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _alerts(factory) -> dict[str, AlertEvent]:
    async with factory() as s:
        rows = (await s.scalars(select(AlertEvent).order_by(AlertEvent.id))).all()
        return {f"{a.kind}:{a.subject}": a for a in rows}


async def test_threshold_alerts_created_once_then_escalate(session, tenant_id, seeded, factory):
    budget = _budget(tenant_id, amount="250")
    session.add(budget)
    await session.commit()
    month = f"{budget.id}/2026-10"

    assert await run_budgets(factory, SETTINGS, now=NOW) == 1
    assert await run_budgets(factory, SETTINGS, now=NOW) == 1  # no duplicates
    alerts = await _alerts(factory)
    assert set(alerts) == {f"budget:{month}/0.8", f"budget_forecast:{month}"}
    warn = alerts[f"budget:{month}/0.8"]
    assert warn.severity == "warning" and warn.resolved_at is None
    assert warn.details["month_to_date"] == "235.00"
    assert alerts[f"budget_forecast:{month}"].severity == "info"

    # Spend crosses 100%: critical alert, forecast alert resolved (MTD is over already).
    session.add(_est(tenant_id, "sub-b", DEV, "dev-2", "gpt-4o-mini", TODAY, 20))
    await session.commit()
    await run_budgets(factory, SETTINGS, now=NOW)
    await run_budgets(factory, SETTINGS, now=NOW)
    alerts = await _alerts(factory)
    assert len(alerts) == 3
    crit = alerts[f"budget:{month}/1.0"]
    assert crit.severity == "critical" and crit.resolved_at is None
    assert alerts[f"budget:{month}/0.8"].resolved_at is None
    assert alerts[f"budget_forecast:{month}"].resolved_at is not None


async def test_forecast_alert_resolves_and_reraises(session, tenant_id, seeded, factory):
    budget = _budget(tenant_id, amount="400", thresholds=[0.8, 1.0])  # forecast 475
    session.add(budget)
    await session.commit()
    key = f"budget_forecast:{budget.id}/2026-10"

    await run_budgets(factory, SETTINGS, now=NOW)
    alerts = await _alerts(factory)
    assert set(alerts) == {key}  # MTD 235 < 320, so no threshold alert yet
    async with factory() as s:
        row = await s.get(AlertEvent, alerts[key].id)
        row.notified_at = NOW
        await s.commit()

    budget.amount = Decimal(500)
    await session.commit()
    await run_budgets(factory, SETTINGS, now=NOW)
    assert (await _alerts(factory))[key].resolved_at is not None

    budget.amount = Decimal(450)
    await session.commit()
    await run_budgets(factory, SETTINGS, now=NOW)
    again = (await _alerts(factory))[key]
    assert again.resolved_at is None and again.notified_at is None


async def test_month_rollover_new_subjects(session, tenant_id, seeded, factory):
    budget = _budget(tenant_id, amount="200")
    session.add(budget)
    await session.commit()
    await run_budgets(factory, SETTINGS, now=NOW)
    oct_keys = {f"budget:{budget.id}/2026-10/0.8", f"budget:{budget.id}/2026-10/1.0"}
    assert set(await _alerts(factory)) == oct_keys

    nov = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    session.add(_est(tenant_id, "sub-a", PROD, "chat", "gpt-4o", date(2026, 11, 1), 170))
    await session.commit()
    await run_budgets(factory, SETTINGS, now=nov)
    alerts = await _alerts(factory)
    assert f"budget:{budget.id}/2026-11/0.8" in alerts
    assert f"budget:{budget.id}/2026-11/1.0" not in alerts
    assert all(alerts[k].resolved_at is not None for k in oct_keys)


async def test_inactive_or_deleted_budget_alerts_resolve(session, tenant_id, seeded, factory):
    budget = _budget(tenant_id, amount="100")
    session.add(budget)
    await session.commit()
    await run_budgets(factory, SETTINGS, now=NOW)
    assert all(a.resolved_at is None for a in (await _alerts(factory)).values())

    budget.active = False
    await session.commit()
    assert await run_budgets(factory, SETTINGS, now=NOW) == 0
    assert all(a.resolved_at is not None for a in (await _alerts(factory)).values())


async def test_run_budgets_without_tenant(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    assert await run_budgets(factory, SETTINGS, now=NOW) == 0


def test_threshold_key():
    assert budgets.threshold_key(0.8) == "0.8"
    assert budgets.threshold_key(1) == "1.0"
