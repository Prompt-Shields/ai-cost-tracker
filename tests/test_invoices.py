"""Invoice-period boundaries and per-period totals (issue #36)."""

from datetime import date
from decimal import Decimal

import pytest

from ai_cost_tracker.db.models import CostLine, DailyEstimate
from ai_cost_tracker.invoices import invoice_periods, period_bounds, recent_periods

D = date


@pytest.mark.parametrize(
    ("day", "billing_day", "expected"),
    [
        # Calendar month (null and 1 are the same).
        (D(2026, 2, 14), None, (D(2026, 2, 1), D(2026, 2, 28))),
        (D(2026, 2, 1), 1, (D(2026, 2, 1), D(2026, 2, 28))),
        (D(2026, 12, 31), None, (D(2026, 12, 1), D(2026, 12, 31))),
        (D(2028, 2, 29), None, (D(2028, 2, 1), D(2028, 2, 29))),  # leap year
        # Mid-month day.
        (D(2026, 3, 15), 15, (D(2026, 3, 15), D(2026, 4, 14))),
        (D(2026, 3, 14), 15, (D(2026, 2, 15), D(2026, 3, 14))),
        (D(2026, 1, 3), 15, (D(2025, 12, 15), D(2026, 1, 14))),  # crosses the year
        # Day 28: the period after February 28 starts on March 28.
        (D(2026, 2, 28), 28, (D(2026, 2, 28), D(2026, 3, 27))),
        (D(2026, 2, 27), 28, (D(2026, 1, 28), D(2026, 2, 27))),
        (D(2026, 3, 31), 28, (D(2026, 3, 28), D(2026, 4, 27))),
        (D(2028, 2, 29), 28, (D(2028, 2, 28), D(2028, 3, 27))),  # leap day inside
        (D(2028, 2, 27), 28, (D(2028, 1, 28), D(2028, 2, 27))),
    ],
)
def test_period_bounds(day, billing_day, expected):
    assert period_bounds(day, billing_day) == expected


@pytest.mark.parametrize("bad", [0, 29, 31, -1])
def test_period_bounds_rejects_bad_day(bad):
    with pytest.raises(ValueError):
        period_bounds(D(2026, 1, 1), bad)


@pytest.mark.parametrize("billing_day", [None, 1, 7, 15, 28])
def test_recent_periods_are_contiguous(billing_day):
    periods = recent_periods(D(2026, 10, 16), billing_day, 14)
    assert periods[0][0] <= D(2026, 10, 16) <= periods[0][1]
    for newer, older in zip(periods, periods[1:], strict=False):
        assert (newer[0] - older[1]).days == 1
        assert older[0].day == (billing_day or 1)


def _line(tenant_id, day, cost, account="sub-1", meter="m", currency="EUR"):
    return CostLine(
        tenant_id=tenant_id,
        cloud="azure",
        sub_account_id=account,
        resource_id="r1",
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency=currency,
    )


def _estimate(tenant_id, day, cost, account="sub-1", deployment="d1"):
    return DailyEstimate(
        tenant_id=tenant_id,
        cloud="azure",
        account_id=account,
        resource_id="r1",
        deployment=deployment,
        day=day,
        estimated_cost=Decimal(cost),
        currency="EUR",
    )


async def test_totals_per_period(session, tenant_id):
    session.add_all(
        [
            # Closed period 2026-08-15..2026-09-14.
            _line(tenant_id, D(2026, 8, 15), "10.50"),
            _line(tenant_id, D(2026, 8, 15), "1.25", meter="m2"),
            _line(tenant_id, D(2026, 9, 14), "2"),
            # Closed period 2026-09-15..2026-10-14.
            _line(tenant_id, D(2026, 9, 15), "3"),
            _line(tenant_id, D(2026, 10, 14), "4"),
            # Open period: billed through 10-14 only, estimates fill the rest.
            _line(tenant_id, D(2026, 10, 15), "5"),
            _estimate(tenant_id, D(2026, 10, 15), "99"),  # ignored: day already billed
            _estimate(tenant_id, D(2026, 10, 16), "0.75"),
            _estimate(tenant_id, D(2026, 10, 16), "0.25", deployment="d2"),
            # Other subscription and other tenant never count.
            _line(tenant_id, D(2026, 10, 15), "1000", account="sub-2"),
        ]
    )
    await session.commit()

    periods = await invoice_periods(session, tenant_id, "sub-1", 15, D(2026, 10, 16), 3)
    assert [(p.start, p.end, p.status) for p in periods] == [
        (D(2026, 10, 15), D(2026, 11, 14), "open"),
        (D(2026, 9, 15), D(2026, 10, 14), "closed"),
        (D(2026, 8, 15), D(2026, 9, 14), "closed"),
    ]
    assert [(p.billed_cost, p.estimated_cost) for p in periods] == [
        (Decimal(5), Decimal(1)),
        (Decimal(7), Decimal(0)),
        (Decimal("13.75"), Decimal(0)),
    ]
    assert {p.currency for p in periods} == {"EUR"}


async def test_calendar_month_when_unset(session, tenant_id):
    session.add_all([_line(tenant_id, D(2026, 9, 30), "1"), _line(tenant_id, D(2026, 10, 1), "2")])
    await session.commit()
    periods = await invoice_periods(session, tenant_id, "sub-1", None, D(2026, 10, 16), 2)
    assert [(p.start, p.end, p.billed_cost) for p in periods] == [
        (D(2026, 10, 1), D(2026, 10, 31), Decimal(2)),
        (D(2026, 9, 1), D(2026, 9, 30), Decimal(1)),
    ]


async def test_empty_defaults_to_usd(session, tenant_id):
    periods = await invoice_periods(session, tenant_id, "sub-1", 1, D(2026, 10, 16), 1)
    assert periods[0].currency == "USD" and periods[0].billed_cost == 0
