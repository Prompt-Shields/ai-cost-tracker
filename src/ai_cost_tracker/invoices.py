"""Invoice-period view per subscription (ARCHITECTURE.md §4.5, issue #36).

A pay-as-you-go invoice period follows the subscription's sign-up day, not the calendar
month. ``CloudAccountRow.billing_period_day`` (1-28) holds that day; null means the
subscription bills by calendar month (period day 1). Days 29-31 aren't allowed so every
month has the start day.

Credits are not cost: the totals here are the pre-credit cost from Cost Management. Azure
credits or sponsorship offsets reduce the invoice amount but not the usage cost, so for a
subscription with credits these totals match the invoice *before* credits are applied.

Per day, billed cost (CostLine) is the cost once any line has arrived for that subscription
and day; days with no billed cost yet fall back to the token x price estimate.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import CostLine, DailyEstimate

DEFAULT_CURRENCY = "USD"
MAX_PERIOD_DAY = 28


@dataclass(frozen=True)
class InvoicePeriod:
    start: date
    end: date  # inclusive
    billed_cost: Decimal
    estimated_cost: Decimal
    currency: str
    status: str  # "open" | "closed"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def period_bounds(day: date, billing_period_day: int | None) -> tuple[date, date]:
    """(start, end inclusive) of the invoice period containing ``day``."""
    d = 1 if billing_period_day is None else billing_period_day
    if not 1 <= d <= MAX_PERIOD_DAY:
        raise ValueError(f"billing_period_day must be 1..{MAX_PERIOD_DAY}, got {d}")
    year, month = (day.year, day.month) if day.day >= d else _shift_month(day.year, day.month, -1)
    start = date(year, month, d)
    next_year, next_month = _shift_month(year, month, 1)
    return start, date(next_year, next_month, d) - timedelta(days=1)


def recent_periods(
    today: date, billing_period_day: int | None, count: int
) -> list[tuple[date, date]]:
    """The ``count`` most recent periods, newest (the open one containing ``today``) first."""
    periods: list[tuple[date, date]] = []
    day = today
    for _ in range(count):
        start, end = period_bounds(day, billing_period_day)
        periods.append((start, end))
        day = start - timedelta(days=1)
    return periods


async def invoice_periods(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: str,
    billing_period_day: int | None,
    today: date,
    count: int,
) -> list[InvoicePeriod]:
    """Billed and estimated cost per period for one subscription, newest first."""
    bounds = recent_periods(today, billing_period_day, count)
    if not bounds:
        return []
    lo, hi = bounds[-1][0], min(bounds[0][1], today)

    billed: dict[date, Decimal] = {}
    currencies: set[str] = set()
    rows = await session.execute(
        select(
            CostLine.charge_period_start,
            CostLine.billing_currency,
            func.sum(CostLine.billed_cost),
        )
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.sub_account_id == account_id,
            CostLine.charge_period_start.between(lo, hi),
        )
        .group_by(CostLine.charge_period_start, CostLine.billing_currency)
    )
    for day, cur, total in rows:
        day = _as_date(day)
        billed[day] = billed.get(day, Decimal(0)) + _dec(total)
        currencies.add(cur)

    estimated: dict[date, Decimal] = {}
    rows = await session.execute(
        select(DailyEstimate.day, DailyEstimate.currency, func.sum(DailyEstimate.estimated_cost))
        .where(
            DailyEstimate.tenant_id == tenant_id,
            DailyEstimate.account_id == account_id,
            DailyEstimate.day.between(lo, hi),
        )
        .group_by(DailyEstimate.day, DailyEstimate.currency)
    )
    estimate_currencies: set[str] = set()
    for day, cur, total in rows:
        day = _as_date(day)
        if day in billed:  # Billed cost wins for any day it has arrived.
            continue
        estimated[day] = estimated.get(day, Decimal(0)) + _dec(total)
        estimate_currencies.add(cur)

    currency = _one(currencies) or _one(estimate_currencies) or DEFAULT_CURRENCY
    result = []
    for start, end in bounds:
        result.append(
            InvoicePeriod(
                start=start,
                end=end,
                billed_cost=sum((v for d, v in billed.items() if start <= d <= end), Decimal(0)),
                estimated_cost=sum(
                    (v for d, v in estimated.items() if start <= d <= end), Decimal(0)
                ),
                currency=currency,
                status="open" if end >= today else "closed",
            )
        )
    return result


def _one(values: set[str]) -> str | None:
    """The single currency, or the default when mixed (v1 bills in one currency)."""
    if not values:
        return None
    return next(iter(values)) if len(values) == 1 else DEFAULT_CURRENCY


def _as_date(value: object) -> date:
    return date.fromisoformat(value) if isinstance(value, str) else value  # type: ignore[return-value]


def _dec(value: object) -> Decimal:
    # SQLite may hand back floats for SUM(); go through str() to avoid binary noise.
    return Decimal(str(value or 0))
