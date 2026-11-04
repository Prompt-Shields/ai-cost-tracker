from datetime import date
from decimal import Decimal

from sqlalchemy import select

from ai_cost_tracker.db.models import (
    AlertEvent,
    CostLine,
    DailyEstimate,
    FxRate,
    Reconciliation,
)
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.normalise.azure_meters import parse_meter
from ai_cost_tracker.reconcile import day_status, reconcile
from tests.test_estimate import END, START, seed_fake, seed_prices

DAY = date(2025, 3, 3)


def _cost_line(tenant_id, sku_meter, cost, day=DAY, account="sub", resource="r"):
    info = parse_meter(sku_meter)
    return CostLine(
        tenant_id=tenant_id,
        cloud="azure",
        sub_account_id=account,
        resource_id=resource,
        charge_period_start=day,
        sku_meter=sku_meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
        ai_model=info.model if info else None,
        ai_direction=info.direction if info else None,
    )


def _estimate(tenant_id, cost, day=DAY, model="gpt-4o", deployment="d"):
    return DailyEstimate(
        tenant_id=tenant_id,
        cloud="azure",
        account_id="sub",
        resource_id="r",
        deployment=deployment,
        model=model,
        day=day,
        estimated_cost=Decimal(cost),
    )


def test_day_status():
    today = date(2025, 3, 15)
    assert day_status(False, date(2024, 1, 1), today) == "estimated"
    assert day_status(True, date(2025, 3, 1), today) == "billed"
    assert day_status(True, date(2025, 2, 1), today) == "billed"
    assert day_status(True, date(2025, 1, 31), today) == "finalised"
    assert day_status(True, date(2024, 11, 30), date(2025, 1, 5)) == "finalised"
    assert day_status(True, date(2024, 12, 1), date(2025, 1, 5)) == "billed"


async def test_fake_data_reconciles_within_two_percent(session, tenant_id):
    await seed_prices(session)
    for c in await seed_fake(session, tenant_id):
        session.add(
            _cost_line(
                tenant_id,
                c.sku_meter,
                c.billed_cost,
                c.charge_period_start,
                c.sub_account_id,
                c.resource_id,
            )
        )
    await session.commit()
    await compute_estimates(session, tenant_id, START, END)

    n = await reconcile(session, tenant_id, START, END, today=date(2025, 3, 20))
    assert n == 3 * 7
    rows = list(await session.scalars(select(Reconciliation)))
    assert {r.model for r in rows} == {"gpt-4o", "gpt-4o-mini", "o3-mini"}
    assert all(r.day_status == "billed" for r in rows)
    assert all(abs(r.variance) <= Decimal("0.0001") for r in rows)
    assert await session.scalar(select(AlertEvent)) is None


async def test_statuses_and_unmapped(session, tenant_id):
    session.add_all(
        [
            _estimate(tenant_id, "1.00", day=date(2025, 3, 10)),  # not billed yet
            _estimate(tenant_id, "1.00", day=date(2025, 1, 10)),
            _cost_line(tenant_id, "gpt-4o-0806 Inp glbl", "1.00", day=date(2025, 1, 10)),
            _cost_line(tenant_id, "Mystery Meter Units", "5.00", day=date(2025, 3, 10)),
        ]
    )
    await session.commit()
    assert (
        await reconcile(
            session, tenant_id, date(2025, 1, 1), date(2025, 3, 31), today=date(2025, 3, 12)
        )
        == 3
    )
    rows = {(r.model, r.day): r for r in await session.scalars(select(Reconciliation))}
    est = rows[("gpt-4o", date(2025, 3, 10))]
    assert est.day_status == "estimated" and est.billed_cost is None and est.variance is None
    fin = rows[("gpt-4o", date(2025, 1, 10))]
    assert fin.day_status == "finalised" and fin.variance == 0
    unmapped = rows[("unmapped", date(2025, 3, 10))]
    assert unmapped.billed_cost == Decimal("5") and unmapped.variance == Decimal("-1")
    # Unmapped meters have their own alert; no variance alert for them.
    assert await session.scalar(select(AlertEvent)) is None


async def test_variance_alert_created_then_resolved(session, tenant_id):
    est = _estimate(tenant_id, "1.05")
    session.add_all([est, _cost_line(tenant_id, "gpt-4o-0806 Inp glbl", "1.00")])
    await session.commit()

    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    alert = await session.scalar(select(AlertEvent))
    assert alert.kind == "variance" and alert.subject == "sub/gpt-4o/2025-03-03"
    assert alert.resolved_at is None
    assert alert.details["variance"] == "0.050000"
    assert alert.details["billed_cost"].startswith("1.00")

    # Re-running with the same data updates the same alert, not a new one.
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    assert len(list(await session.scalars(select(AlertEvent)))) == 1

    est.estimated_cost = Decimal("1.01")
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    await session.refresh(alert)
    assert alert.resolved_at is not None
    row = await session.scalar(select(Reconciliation))
    assert row.variance == Decimal("0.01")


async def test_reconcile_is_idempotent(session, tenant_id):
    session.add_all(
        [_estimate(tenant_id, "0.90"), _cost_line(tenant_id, "gpt-4o-0806 Outp glbl", "1")]
    )
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    rows = list(await session.scalars(select(Reconciliation)))
    assert len(rows) == 1 and rows[0].variance == Decimal("-0.1")


# --- Multi-currency (M3, #49/#50): a EUR-billed project with USD estimates. ---

EUR_PER_USD = Decimal("0.92")


def _gcp_cost(tenant_id, cost, currency="EUR", day=DAY, sku="Gemini 2.5 Flash Text Input"):
    return CostLine(
        tenant_id=tenant_id,
        cloud="gcp",
        sub_account_id="proj",
        resource_id="//aiplatform.googleapis.com/projects/proj/locations/us-central1",
        charge_period_start=day,
        sku_meter=sku,
        billed_cost=Decimal(cost),
        billing_currency=currency,
        ai_model="gemini-2.5-flash",
        ai_direction="input",
    )


def _gcp_estimate(tenant_id, cost, day=DAY):
    return DailyEstimate(
        tenant_id=tenant_id,
        cloud="gcp",
        account_id="proj",
        resource_id="//aiplatform.googleapis.com/projects/proj/locations/us-central1",
        deployment="gemini-2.5-flash",
        model="gemini-2.5-flash",
        day=day,
        estimated_cost=Decimal(cost),
        currency="USD",
    )


def _eur_usd(day=DAY, rate=Decimal("1") / EUR_PER_USD):
    return FxRate(day=day, base="EUR", quote="USD", rate=rate.quantize(Decimal("1e-10")))


async def test_eur_billed_matches_usd_estimate_after_conversion(session, tenant_id):
    session.add_all([_gcp_estimate(tenant_id, "10.00"), _gcp_cost(tenant_id, "9.20"), _eur_usd()])
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    row = await session.scalar(select(Reconciliation))
    # Recorded in the billed currency (EUR).
    assert row.billed_cost == Decimal("9.20")
    assert abs(row.estimated_cost - Decimal("9.20")) < Decimal("0.000001")
    assert abs(row.variance) <= Decimal("0.000001")
    assert await session.scalar(select(AlertEvent)) is None


async def test_eur_billed_mismatch_after_conversion_alerts(session, tenant_id):
    session.add_all([_gcp_estimate(tenant_id, "10.00"), _gcp_cost(tenant_id, "8.00"), _eur_usd()])
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    alert = await session.scalar(select(AlertEvent))
    assert alert.subject == "proj/gemini-2.5-flash/2025-03-03"
    assert alert.details["currency"] == "EUR"
    assert alert.details["fx_approximate"] is False
    assert Decimal(alert.details["variance"]) == Decimal("0.15")  # 9.20 vs 8.00 EUR


async def test_stale_rate_is_flagged_approximate(session, tenant_id):
    old = DAY.replace(day=1).replace(month=2)  # >7 days before DAY
    session.add_all(
        [_gcp_estimate(tenant_id, "10.00"), _gcp_cost(tenant_id, "5.00"), _eur_usd(day=old)]
    )
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    alert = await session.scalar(select(AlertEvent))
    assert alert.details["fx_approximate"] is True


async def test_missing_rate_skips_alert(session, tenant_id):
    session.add_all([_gcp_estimate(tenant_id, "10.00"), _gcp_cost(tenant_id, "5.00")])
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    row = await session.scalar(select(Reconciliation))
    assert row.billed_cost == Decimal("5.00") and row.estimated_cost == Decimal("10.00")
    # The amounts are in different currencies, so no (false) variance alert.
    assert await session.scalar(select(AlertEvent)) is None


async def test_unbilled_day_keeps_estimate_currency(session, tenant_id):
    session.add_all([_gcp_estimate(tenant_id, "10.00"), _eur_usd()])
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    row = await session.scalar(select(Reconciliation))
    assert row.estimated_cost == Decimal("10.00") and row.billed_cost is None


async def test_mixed_billing_currencies_use_dominant_one(session, tenant_id):
    session.add_all(
        [
            _gcp_estimate(tenant_id, "10.00"),
            _gcp_cost(tenant_id, "8.28"),  # EUR, dominant
            _gcp_cost(tenant_id, "1.00", currency="USD", sku="Gemini 2.5 Flash Text Output"),
            _eur_usd(),
        ]
    )
    await session.commit()
    await reconcile(session, tenant_id, DAY, DAY, today=DAY)
    row = await session.scalar(select(Reconciliation))
    # 8.28 EUR + 1 USD (0.92 EUR) = 9.20 EUR billed, matching the 10 USD estimate.
    assert abs(row.billed_cost - Decimal("9.20")) < Decimal("0.000001")
    assert abs(row.variance) <= Decimal("0.000001")
