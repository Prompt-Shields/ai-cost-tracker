"""Monthly budgets: month-to-date spend, forecast to month end, and threshold alerts (#29).

Spend is whatever the cost explorer shows (``api/queries.py``): billed cost for account-days
that have billing data, the token x price estimate otherwise, attributed to deployments the
same way (§4.4). Months are UTC calendar months.

Forecast method:

    forecast = spend before today
             + max(today's spend so far, expected(today))
             + sum(expected(d) for every day after today to month end)

``expected(d)`` comes from the last 7 complete days (yesterday and the six before it, which
may reach into last month). Days before the first day the scope has any data in that window
are ignored, so a new deployment or a fresh install isn't dragged down by empty days. When the
history has at least two weekdays and one weekend day, weekdays and weekends are averaged
separately (AI traffic is usually much lower at weekends); otherwise one flat average is used.
With no history at all there is nothing to extrapolate from, so the forecast is the MTD.
Today is partial, which is why it counts as at least its expected spend.
"""

import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api.queries import Filters, Inventory, load, load_inventory
from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import AlertEvent, Budget, Tenant

SCOPE_TYPES = ("all", "account", "model", "deployment", "tag")
ALERT_KIND = "budget"
FORECAST_KIND = "budget_forecast"
HISTORY_DAYS = 7
_ZERO = Decimal(0)
_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Evaluation:
    month_to_date: Decimal
    forecast_month_end: Decimal
    percent_used: float  # MTD / amount x 100
    status: str  # ok | warning | over
    method: str  # weekday_weekend | flat | none (no history): how the forecast was made


def month_bounds(today: date) -> tuple[date, date]:
    """First and last day of ``today``'s calendar month."""
    last = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last)


def month_key(today: date) -> str:
    return f"{today.year:04d}-{today.month:02d}"


def threshold_key(threshold: float) -> str:
    """Stable text for a threshold in alert subjects: 0.8 -> "0.8", 1 -> "1.0"."""
    return str(float(threshold))


def scope_filters(budget: Budget, inventory: Inventory) -> Filters | None:
    """The cost-explorer filters for a budget's scope; None when the scope matches nothing
    (a deployment that no longer exists)."""
    value = budget.scope_value
    match budget.scope_type:
        case "all":
            return Filters()
        case "account":
            return Filters(account_id=value)
        case "model":
            return Filters(model=value)
        case "tag":
            key, _, tag_value = (value or "").partition(":")
            return Filters(tag=(key, tag_value))
        case "deployment":
            row = next((r for r in inventory.rows if str(r.id) == value), None)
            if row is None:
                return None
            return Filters(resource_id=row.resource_id, deployment=row.name)
    raise ValueError(f"unknown scope_type {budget.scope_type!r}")


async def daily_spend(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    filters: Filters,
    inventory: Inventory,
    start: date,
    end: date,
) -> tuple[dict[date, Decimal], set[date]]:
    """Effective spend per day in [start, end], and the days that have any data at all."""
    data = await load(session, tenant_id, start, end, filters, inventory)
    totals: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    seen: set[date] = set()
    for key, cell in data.cells.items():
        totals[key.day] += data.effective(key, cell)[0]
        seen.add(key.day)
    return dict(totals), seen


def forecast(
    totals: dict[date, Decimal], seen: set[date], today: date
) -> tuple[Decimal, Decimal, str]:
    """(month to date, forecast at month end, method). See the module docstring."""
    month_start, month_end = month_bounds(today)
    mtd = sum((v for d, v in totals.items() if month_start <= d <= today), _ZERO)
    history = [today - timedelta(days=i) for i in range(HISTORY_DAYS, 0, -1)]
    observed = [d for d in history if d in seen]
    if not observed:
        return mtd, mtd, "none"
    history = [d for d in history if d >= observed[0]]

    weekdays = [totals.get(d, _ZERO) for d in history if d.weekday() < 5]
    weekends = [totals.get(d, _ZERO) for d in history if d.weekday() >= 5]
    if len(weekdays) >= 2 and len(weekends) >= 1:
        method = "weekday_weekend"
        weekday_avg = sum(weekdays, _ZERO) / len(weekdays)
        weekend_avg = sum(weekends, _ZERO) / len(weekends)
    else:
        method = "flat"
        weekday_avg = weekend_avg = sum(weekdays + weekends, _ZERO) / len(history)

    def expected(d: date) -> Decimal:
        return weekend_avg if d.weekday() >= 5 else weekday_avg

    today_spend = totals.get(today, _ZERO)
    projected = mtd - today_spend + max(today_spend, expected(today))
    d = today + timedelta(days=1)
    while d <= month_end:
        projected += expected(d)
        d += timedelta(days=1)
    return mtd, projected, method


def budget_status(budget: Budget, mtd: Decimal, projected: Decimal) -> str:
    """over: MTD reached the amount; warning: MTD or forecast reached the lowest threshold."""
    amount = Decimal(str(budget.amount))
    if mtd >= amount:
        return "over"
    lowest = Decimal(str(min(budget.thresholds or [1.0])))
    if mtd >= lowest * amount or projected >= lowest * amount:
        return "warning"
    return "ok"


async def evaluate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    budget: Budget,
    today: date,
    inventory: Inventory | None = None,
) -> Evaluation:
    """MTD, forecast and status for one budget as of ``today`` (UTC)."""
    if inventory is None:
        inventory = await load_inventory(session, tenant_id)
    filters = scope_filters(budget, inventory)
    if filters is None:
        totals: dict[date, Decimal] = {}
        seen: set[date] = set()
    else:
        start = min(month_bounds(today)[0], today - timedelta(days=HISTORY_DAYS))
        totals, seen = await daily_spend(session, tenant_id, filters, inventory, start, today)
    mtd, projected, method = forecast(totals, seen, today)
    amount = Decimal(str(budget.amount))
    percent = float((mtd / amount * 100).quantize(_CENTS)) if amount > 0 else 0.0
    return Evaluation(mtd, projected, percent, budget_status(budget, mtd, projected), method)


# --- alert job -------------------------------------------------------------------------


async def run_budgets(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """Raise/refresh/resolve budget alerts for the default tenant. Returns budgets evaluated.

    Idempotent: alerts are keyed by (kind, subject), so re-running updates rows in place.
    Subjects carry the month, so each month alerts afresh; open alerts from earlier months,
    or for budgets that were deleted or deactivated, are resolved.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    month = month_key(today)
    async with session_factory() as session:
        tenant_id = await session.scalar(
            select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
        )
        if tenant_id is None:
            return 0
        budgets = (
            await session.scalars(
                select(Budget).where(Budget.tenant_id == tenant_id, Budget.active.is_(True))
            )
        ).all()
        alerts = await _existing_alerts(session, tenant_id, month)
        inventory = await load_inventory(session, tenant_id)
        wanted: set[tuple[str, str]] = set()  # (kind, subject) that should stay open

        for budget in budgets:
            ev = await evaluate(session, tenant_id, budget, today, inventory)
            amount = Decimal(str(budget.amount))
            for threshold in sorted({float(t) for t in budget.thresholds or []}):
                subject = f"{budget.id}/{month}/{threshold_key(threshold)}"
                if ev.month_to_date < Decimal(str(threshold)) * amount:
                    continue
                wanted.add((ALERT_KIND, subject))
                _raise(
                    session,
                    alerts,
                    tenant_id,
                    ALERT_KIND,
                    subject,
                    severity="critical" if threshold >= 1.0 else "warning",
                    message=(
                        f"Budget '{budget.name}' has used {ev.percent_used:g}% of "
                        f"{_fmt(amount)} {budget.currency} this month "
                        f"({_fmt(ev.month_to_date)} spent, {threshold:.0%} threshold)"
                    ),
                    details=_details(budget, ev, month, threshold=threshold),
                )
            if ev.forecast_month_end > amount and ev.month_to_date < amount:
                subject = f"{budget.id}/{month}"
                wanted.add((FORECAST_KIND, subject))
                _raise(
                    session,
                    alerts,
                    tenant_id,
                    FORECAST_KIND,
                    subject,
                    severity="info",
                    message=(
                        f"Budget '{budget.name}' is forecast to reach "
                        f"{_fmt(ev.forecast_month_end)} {budget.currency} by month end, over "
                        f"its {_fmt(amount)} budget ({_fmt(ev.month_to_date)} spent so far)"
                    ),
                    details=_details(budget, ev, month),
                )

        for key, alert in alerts.items():
            if key not in wanted and alert.resolved_at is None:
                alert.resolved_at = now
        await session.commit()
        return len(budgets)


async def _existing_alerts(
    session: AsyncSession, tenant_id: uuid.UUID, month: str
) -> dict[tuple[str, str], AlertEvent]:
    """This month's budget alerts plus any still-open ones (so they can be resolved)."""
    rows = await session.scalars(
        select(AlertEvent).where(
            AlertEvent.tenant_id == tenant_id,
            AlertEvent.kind.in_((ALERT_KIND, FORECAST_KIND)),
            or_(AlertEvent.resolved_at.is_(None), AlertEvent.subject.contains(f"/{month}")),
        )
    )
    return {(a.kind, a.subject): a for a in rows}


def _raise(
    session: AsyncSession,
    alerts: dict[tuple[str, str], AlertEvent],
    tenant_id: uuid.UUID,
    kind: str,
    subject: str,
    *,
    severity: str,
    message: str,
    details: dict,
) -> None:
    """Create or refresh one alert. A resolved alert that comes back is re-sent."""
    alert = alerts.get((kind, subject))
    if alert is None:
        alert = AlertEvent(tenant_id=tenant_id, kind=kind, subject=subject)
        session.add(alert)
        alerts[(kind, subject)] = alert
    elif alert.resolved_at is not None:
        alert.resolved_at = None
        alert.notified_at = None
    alert.severity = severity
    alert.message = message
    alert.details = details


def _details(budget: Budget, ev: Evaluation, month: str, threshold: float | None = None) -> dict:
    out = {
        "budget_id": str(budget.id),
        "budget_name": budget.name,
        "scope_type": budget.scope_type,
        "scope_value": budget.scope_value,
        "month": month,
        "amount": _fmt(Decimal(str(budget.amount))),
        "currency": budget.currency,
        "month_to_date": _fmt(ev.month_to_date),
        "forecast_month_end": _fmt(ev.forecast_month_end),
        "percent_used": ev.percent_used,
        "forecast_method": ev.method,
    }
    if threshold is not None:
        out["threshold"] = threshold
    return out


def _fmt(value: Decimal) -> str:
    return format(value.quantize(_CENTS), "f")
