"""Reconcile estimated against billed cost per account, model and day (issue #21).

Pay-as-you-go pays retail (ARCHITECTURE.md §4.5), so a gap above 2% between the estimate and
the bill is a bug to investigate, not a calibration factor: it raises a "variance" alert,
which resolves itself once a later run (fresh prices, late billing rows) closes the gap.

Billed cost includes every meter parsed to the model (training/hosting too); meters the
parser doesn't know land in model "unmapped". Those get no variance alert because there is
nothing to estimate them from, and unmapped meters are surfaced separately.

Estimates are priced in USD, but an account can be billed in another currency (a GCP project
billed in EUR). Every amount for a (cloud, account, day) is therefore converted into that
day's billing currency before comparing, so a currency difference never reads as variance.
Days without billed rows keep the estimate's own currency. When no FX rate exists at all the
comparison is meaningless, so no variance alert is raised or resolved for that key.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import AlertEvent, CostLine, DailyEstimate, Reconciliation
from ai_cost_tracker.estimate import normalise_model
from ai_cost_tracker.fx import FxBook, load_fx_book

UNMAPPED = "unmapped"
VARIANCE_THRESHOLD = Decimal("0.02")
ALERT_KIND = "variance"
_VAR_QUANT = Decimal("0.000001")

_Key = tuple[str, str, str, date]  # cloud, account, model, day
_AccountDay = tuple[str, str, date]  # cloud, account, day
_ByCurrency = dict[_Key, dict[str, Decimal]]


def day_status(has_billed: bool, day: date, today: date) -> str:
    """estimated: no billed rows yet; finalised: billed and before last calendar month."""
    if not has_billed:
        return "estimated"
    first_of_month = today.replace(day=1)
    prev_month_start = (
        first_of_month.replace(year=first_of_month.year - 1, month=12)
        if first_of_month.month == 1
        else first_of_month.replace(month=first_of_month.month - 1)
    )
    return "finalised" if day < prev_month_start else "billed"


async def reconcile(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    today: date | None = None,
) -> int:
    """Replace Reconciliation rows for [start, end] and update variance alerts; returns rows."""
    today = today or datetime.now(UTC).date()
    estimated_raw, price_missing = await _estimated(session, tenant_id, start, end)
    billed_raw = await _billed(session, tenant_id, start, end)
    currencies = _billing_currencies(billed_raw)
    estimated, billed, fx_approx, fx_missing = await _to_billing_currency(
        session, start, end, estimated_raw, billed_raw, currencies
    )

    await session.execute(
        delete(Reconciliation).where(
            Reconciliation.tenant_id == tenant_id,
            Reconciliation.day >= start,
            Reconciliation.day <= end,
        )
    )
    keys = sorted(set(estimated) | set(billed))
    alerts = await _existing_alerts(session, tenant_id, {_subject(k) for k in keys})
    now = datetime.now(UTC)
    for key in keys:
        cloud, account, model, day = key
        est = estimated.get(key, Decimal(0))
        bill = billed.get(key)
        variance = ((est - bill) / bill).quantize(_VAR_QUANT) if bill else None
        session.add(
            Reconciliation(
                tenant_id=tenant_id,
                cloud=cloud,
                account_id=account,
                model=model,
                day=day,
                estimated_cost=est,
                billed_cost=bill,
                variance=variance,
                day_status=day_status(bill is not None, day, today),
                currency=currencies.get((cloud, account, day), "USD"),
            )
        )
        # An estimate with no price is incomplete, not wrong: alerting on it would just
        # repeat the price_missing flag on every day.
        # Same for a missing FX rate: the amounts are in different currencies.
        if variance is None or model == UNMAPPED or key in price_missing or key in fx_missing:
            continue
        subject = _subject(key)
        alert = alerts.get(subject)
        if abs(variance) > VARIANCE_THRESHOLD:
            details = {
                "cloud": cloud,
                "account_id": account,
                "model": model,
                "day": day.isoformat(),
                "estimated_cost": str(est),
                "billed_cost": str(bill),
                "variance": str(variance),
                "price_missing": key in price_missing,
                "currency": currencies.get((cloud, account, day), "USD"),
                "fx_approximate": key in fx_approx,
            }
            message = (
                f"Estimated cost for {model} on {day.isoformat()} ({account}) differs from "
                f"billed cost by {variance * 100:.2f}%"
            )
            if alert is None:
                alert = AlertEvent(tenant_id=tenant_id, kind=ALERT_KIND, subject=subject)
                session.add(alert)
                alerts[subject] = alert
            alert.message = message
            alert.details = details
            alert.resolved_at = None
        elif alert is not None and alert.resolved_at is None:
            alert.resolved_at = now
    await session.commit()
    return len(keys)


async def _estimated(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> tuple[_ByCurrency, set[_Key]]:
    totals: _ByCurrency = {}
    missing: set[_Key] = set()
    rows = await session.scalars(
        select(DailyEstimate).where(
            DailyEstimate.tenant_id == tenant_id,
            DailyEstimate.day >= start,
            DailyEstimate.day <= end,
        )
    )
    for r in rows:
        key = (r.cloud, r.account_id, normalise_model(r.model, r.cloud) or UNMAPPED, r.day)
        by_ccy = totals.setdefault(key, {})
        ccy = (r.currency or "USD").upper()
        by_ccy[ccy] = by_ccy.get(ccy, Decimal(0)) + Decimal(r.estimated_cost)
        if r.price_missing:
            missing.add(key)
    return totals, missing


async def _billed(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> _ByCurrency:
    rows = await session.execute(
        select(
            CostLine.cloud,
            CostLine.sub_account_id,
            CostLine.ai_model,
            CostLine.charge_period_start,
            CostLine.billing_currency,
            func.sum(CostLine.billed_cost),
        )
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.charge_period_start >= start,
            CostLine.charge_period_start <= end,
            # PTU capacity is billed per hour, not per token, so token estimates can't match
            # it; comparing would raise a variance alert every day (see ptu.py instead).
            or_(CostLine.ai_direction.is_(None), CostLine.ai_direction != "provisioned"),
        )
        .group_by(
            CostLine.cloud,
            CostLine.sub_account_id,
            CostLine.ai_model,
            CostLine.charge_period_start,
            CostLine.billing_currency,
        )
    )
    totals: _ByCurrency = {}
    for cloud, account, model, day, currency, cost in rows:
        key = (cloud, account, normalise_model(model, cloud) or UNMAPPED, day)
        by_ccy = totals.setdefault(key, {})
        ccy = (currency or "USD").upper()
        by_ccy[ccy] = by_ccy.get(ccy, Decimal(0)) + Decimal(cost or 0)
    return totals


def _billing_currencies(billed: _ByCurrency) -> dict[_AccountDay, str]:
    """The currency each (cloud, account, day) is billed in.

    One per account in practice; if lines ever mix currencies, the one carrying the most
    cost wins (ties alphabetical) and the rest are converted into it.
    """
    weight: dict[_AccountDay, dict[str, Decimal]] = {}
    for (cloud, account, _model, day), by_ccy in billed.items():
        w = weight.setdefault((cloud, account, day), {})
        for ccy, cost in by_ccy.items():
            w[ccy] = w.get(ccy, Decimal(0)) + abs(cost)
    return {k: min(w, key=lambda c: (-w[c], c)) for k, w in weight.items()}


async def _to_billing_currency(
    session: AsyncSession,
    start: date,
    end: date,
    estimated: _ByCurrency,
    billed: _ByCurrency,
    currencies: dict[_AccountDay, str],
) -> tuple[dict[_Key, Decimal], dict[_Key, Decimal], set[_Key], set[_Key]]:
    """Sum each key's amounts in its account-day billing currency.

    Returns (estimated, billed, keys with an approximate rate, keys with no rate). Rates are
    only loaded when some amount actually needs converting, so single-currency tenants keep
    exact, unquantized sums.
    """
    needs_fx = any(
        ccy != currencies.get((k[0], k[1], k[3]), ccy)
        for src in (estimated, billed)
        for k, by_ccy in src.items()
        for ccy in by_ccy
    )
    book = await load_fx_book(session, start, end) if needs_fx else FxBook()
    approx: set[_Key] = set()
    missing: set[_Key] = set()

    def collapse(src: _ByCurrency) -> dict[_Key, Decimal]:
        out: dict[_Key, Decimal] = {}
        for key, by_ccy in src.items():
            cloud, account, _model, day = key
            target = currencies.get((cloud, account, day))
            total = Decimal(0)
            for ccy, amount in sorted(by_ccy.items()):
                if target is None or ccy == target:
                    total += amount
                    continue
                if book.rate(day, ccy, target) is None:
                    missing.add(key)
                converted, is_approx = book.convert(amount, day, ccy, target)
                if is_approx:
                    approx.add(key)
                total += converted
            out[key] = total
        return out

    return collapse(estimated), collapse(billed), approx, missing


def _subject(key: _Key) -> str:
    _cloud, account, model, day = key
    return f"{account}/{model}/{day.isoformat()}"


async def _existing_alerts(
    session: AsyncSession, tenant_id: uuid.UUID, subjects: set[str]
) -> dict[str, AlertEvent]:
    out: dict[str, AlertEvent] = {}
    pending = sorted(subjects)
    for i in range(0, len(pending), 500):  # Keep IN lists well under driver limits.
        rows = await session.scalars(
            select(AlertEvent).where(
                AlertEvent.tenant_id == tenant_id,
                AlertEvent.kind == ALERT_KIND,
                AlertEvent.subject.in_(pending[i : i + 500]),
            )
        )
        out.update({a.subject: a for a in rows})
    return out
