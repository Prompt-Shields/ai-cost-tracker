"""Reconcile estimated against billed cost per account, model and day (issue #21).

Pay-as-you-go pays retail (ARCHITECTURE.md §4.5), so a gap above 2% between the estimate and
the bill is a bug to investigate, not a calibration factor: it raises a "variance" alert,
which resolves itself once a later run (fresh prices, late billing rows) closes the gap.

Billed cost includes every meter parsed to the model (training/hosting too); meters the
parser doesn't know land in model "unmapped". Those get no variance alert because there is
nothing to estimate them from, and unmapped meters are surfaced separately.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import AlertEvent, CostLine, DailyEstimate, Reconciliation
from ai_cost_tracker.estimate import normalise_model

UNMAPPED = "unmapped"
VARIANCE_THRESHOLD = Decimal("0.02")
ALERT_KIND = "variance"
_VAR_QUANT = Decimal("0.000001")

_Key = tuple[str, str, str, date]  # cloud, account, model, day


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
    estimated, price_missing = await _estimated(session, tenant_id, start, end)
    billed = await _billed(session, tenant_id, start, end)

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
            )
        )
        if variance is None or model == UNMAPPED:
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
) -> tuple[dict[_Key, Decimal], set[_Key]]:
    totals: dict[_Key, Decimal] = {}
    missing: set[_Key] = set()
    rows = await session.scalars(
        select(DailyEstimate).where(
            DailyEstimate.tenant_id == tenant_id,
            DailyEstimate.day >= start,
            DailyEstimate.day <= end,
        )
    )
    for r in rows:
        key = (r.cloud, r.account_id, normalise_model(r.model) or UNMAPPED, r.day)
        totals[key] = totals.get(key, Decimal(0)) + Decimal(r.estimated_cost)
        if r.price_missing:
            missing.add(key)
    return totals, missing


async def _billed(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> dict[_Key, Decimal]:
    rows = await session.execute(
        select(
            CostLine.cloud,
            CostLine.sub_account_id,
            CostLine.ai_model,
            CostLine.charge_period_start,
            func.sum(CostLine.billed_cost),
        )
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.charge_period_start >= start,
            CostLine.charge_period_start <= end,
        )
        .group_by(
            CostLine.cloud,
            CostLine.sub_account_id,
            CostLine.ai_model,
            CostLine.charge_period_start,
        )
    )
    totals: dict[_Key, Decimal] = {}
    for cloud, account, model, day, cost in rows:
        key = (cloud, account, normalise_model(model) or UNMAPPED, day)
        totals[key] = totals.get(key, Decimal(0)) + Decimal(cost or 0)
    return totals


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
