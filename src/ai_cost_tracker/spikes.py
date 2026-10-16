"""Spike detection on daily and intraday spend per deployment (issue #31).

Two jobs, both idempotent and both writing AlertEvents only (the notifier sends them):

* ``run_spike_detection`` (daily): yesterday's cost per deployment against two baselines,
  the median of the same weekday over the previous four weeks and the median of the
  previous seven days. Cost is billed once the day is billed, else the token estimate,
  using the same per-deployment attribution as the cost API (``api.queries``, §4.4), so the
  alert agrees with what the dashboard shows. Billed cost no deployment serves
  ("unattributed") is skipped: it has no deployment to blame. A spike is cost above
  ``SPIKE_RATIO`` x the larger baseline *and* more than ``DAILY_FLOOR`` above it, so tiny
  deployments that go from $0.10 to $1 don't page anyone.
* ``run_intraday_spikes`` (every 15 minutes): tokens over the last two complete hours
  against the median of the same hours on the same weekday over the previous four weeks.
  Tokens, not cost, because estimates are only computed per day.

No floods: one alert per deployment, day and kind (stable subject
``<resource_id>/<deployment>/<YYYY-MM-DD>``). Re-runs update that row; ``notified_at`` is
reset only when things get clearly worse (daily: warning -> critical; intraday: ratio up
by ``INTRADAY_RENOTIFY_GROWTH`` since the last notification).

Deployments with less than ``MIN_HISTORY_DAYS`` of data get no alerts: their baseline would
be a guess, and a new deployment ramping up is not a spike.
"""

import logging
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api import queries
from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import AlertEvent, Tenant, TokenUsageRow

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

KIND_DAILY = "spike"
KIND_INTRADAY = "spike_intraday"

SPIKE_RATIO = Decimal(2)  # raise above 2x baseline
CRITICAL_RATIO = Decimal(5)  # critical above 5x baseline
DAILY_FLOOR = Decimal(5)  # and at least $5/day above baseline (billing currency)
MIN_HISTORY_DAYS = 7
BASELINE_WEEKS = 4

INTRADAY_HOURS = 2
# Below this many tokens in the window a jump is noise (a few test calls), not a spike.
INTRADAY_FLOOR_TOKENS = 100_000
INTRADAY_RENOTIFY_GROWTH = 1.5

_RATIO_PLACES = Decimal("0.01")

DeploymentKey = tuple[str, str]  # resource_id, deployment


# --- shared ----------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    # SQLite hands back naive datetimes for timezone-aware columns.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now(now: datetime | None) -> datetime:
    return _utc(now or datetime.now(UTC))


def _subject(key: DeploymentKey, day: date) -> str:
    return f"{key[0]}/{key[1]}/{day.isoformat()}"


async def _tenant(session: AsyncSession, settings: Settings) -> uuid.UUID | None:
    """The v1 single tenant; None before anything was ingested (nothing to check)."""
    return await session.scalar(
        select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
    )


async def _alerts(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, subjects: set[str]
) -> dict[str, AlertEvent]:
    out: dict[str, AlertEvent] = {}
    pending = sorted(subjects)
    for i in range(0, len(pending), 500):  # Keep IN lists well under driver limits.
        rows = await session.scalars(
            select(AlertEvent).where(
                AlertEvent.tenant_id == tenant_id,
                AlertEvent.kind == kind,
                AlertEvent.subject.in_(pending[i : i + 500]),
            )
        )
        out.update({a.subject: a for a in rows})
    return out


def _severity(ratio: Decimal | None) -> str:
    """None ratio means a zero baseline: anything over the floor is as bad as it gets."""
    return "critical" if ratio is None or ratio > CRITICAL_RATIO else "warning"


def _ratio(value: Decimal, baseline: Decimal) -> Decimal | None:
    return (value / baseline).quantize(_RATIO_PLACES) if baseline > 0 else None


def _manually_resolved(alert: AlertEvent) -> bool:
    """Someone closed it in the inbox (api/routes/alerts.py): don't reopen it."""
    return alert.resolved_at is not None and bool((alert.details or {}).get("resolved_by"))


def _median(values: list[Decimal]) -> Decimal:
    return Decimal(statistics.median(values)) if values else Decimal(0)


# --- daily -----------------------------------------------------------------------------


def daily_baselines(
    series: dict[date, Decimal], day: date, first_seen: date
) -> tuple[Decimal, Decimal] | None:
    """(same-weekday median, previous-7-day median) for ``day``; None without enough history.

    Days after ``first_seen`` with no rows count as zero spend (the deployment was idle);
    days before it are not history at all.
    """
    if (day - first_seen).days < MIN_HISTORY_DAYS:
        return None
    weekday = [
        series.get(day - timedelta(weeks=w), Decimal(0))
        for w in range(1, BASELINE_WEEKS + 1)
        if day - timedelta(weeks=w) >= first_seen
    ]
    week = [series.get(day - timedelta(days=d), Decimal(0)) for d in range(1, 8)]
    return _median(weekday), _median(week)


async def _daily_costs(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> dict[DeploymentKey, dict[date, Decimal]]:
    """Effective (billed, else estimated) cost per deployment and day, as the cost API has it."""
    inventory = await queries.load_inventory(session, tenant_id)
    data = await queries.load(session, tenant_id, start, end, queries.Filters(), inventory)
    out: dict[DeploymentKey, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for key, cell in data.cells.items():
        if key.deployment is None or not cell.has_cost:
            continue
        total, _inp, _out = data.effective(key, cell)
        out[(key.resource_id, key.deployment)][key.day] += total
    return out


async def detect_daily(
    session: AsyncSession, tenant_id: uuid.UUID, day: date, now: datetime
) -> int:
    """Raise/refresh/resolve "spike" alerts for ``day``; returns alerts raised or refreshed."""
    start = day - timedelta(weeks=BASELINE_WEEKS)
    costs = await _daily_costs(session, tenant_id, start, day)
    currency = await queries.currency(session, tenant_id, start, day)
    subjects = {_subject(k, day) for k in costs}
    alerts = await _alerts(session, tenant_id, KIND_DAILY, subjects)
    raised = 0
    for key, series in sorted(costs.items()):
        subject = _subject(key, day)
        alert = alerts.get(subject)
        cost = series.get(day, Decimal(0))
        first_seen = min(d for d, v in series.items() if d <= day)
        baselines = daily_baselines(series, day, first_seen)
        baseline = max(baselines) if baselines else Decimal(0)
        spiking = (
            baselines is not None
            and cost > baseline * SPIKE_RATIO
            and cost - baseline > DAILY_FLOOR
        )
        if not spiking:
            # A later run (late billing rows, restated estimate) can clear the spike.
            if alert is not None and alert.resolved_at is None:
                alert.resolved_at = now
            continue
        assert baselines is not None
        ratio = _ratio(cost, baseline)
        severity = _severity(ratio)
        resource, deployment = key
        label = queries.deployment_label(resource, deployment)
        times = f"{ratio}x" if ratio is not None else "from nothing"
        message = (
            f"{label} spent {cost.quantize(Decimal('0.01'))} {currency} on {day.isoformat()}, "
            f"{times} its usual {baseline.quantize(Decimal('0.01'))} {currency}"
        )
        details = {
            "resource_id": resource,
            "deployment": deployment,
            "day": day.isoformat(),
            "cost": str(cost),
            "currency": currency,
            "baseline_same_weekday": str(baselines[0]),
            "baseline_previous_7_days": str(baselines[1]),
            "baseline": str(baseline),
            "ratio": str(ratio) if ratio is not None else None,
        }
        if alert is None:
            alert = AlertEvent(tenant_id=tenant_id, kind=KIND_DAILY, subject=subject)
            session.add(alert)
        elif _manually_resolved(alert):
            continue
        elif alert.notified_at is not None and (
            alert.resolved_at is not None
            or (severity == "critical" and alert.severity != "critical")
        ):
            alert.notified_at = None  # came back, or got worse: tell people again
        alert.message = message
        alert.details = details
        alert.severity = severity
        alert.resolved_at = None
        raised += 1
    await session.commit()
    return raised


async def run_spike_detection(
    session_factory: SessionFactory, settings: Settings, *, now: datetime | None = None
) -> int:
    """Daily job: check yesterday (UTC). Returns spike alerts raised or refreshed."""
    now = _now(now)
    async with session_factory() as session:
        tenant_id = await _tenant(session, settings)
        if tenant_id is None:
            return 0
        return await detect_daily(session, tenant_id, now.date() - timedelta(days=1), now)


# --- intraday --------------------------------------------------------------------------


async def _window_tokens(
    session: AsyncSession, tenant_id: uuid.UUID, lo: datetime, hi: datetime
) -> dict[DeploymentKey, int]:
    rows = await session.execute(
        select(
            TokenUsageRow.resource_id,
            TokenUsageRow.deployment,
            func.sum(TokenUsageRow.input_tokens + TokenUsageRow.output_tokens),
        )
        .where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.bucket_start >= lo,
            TokenUsageRow.bucket_start < hi,
        )
        .group_by(TokenUsageRow.resource_id, TokenUsageRow.deployment)
    )
    return {(r, d): int(t or 0) for r, d, t in rows}


async def _first_seen(
    session: AsyncSession, tenant_id: uuid.UUID, since: datetime
) -> dict[DeploymentKey, datetime]:
    """Oldest bucket per deployment, looking back only as far as the baseline needs."""
    rows = await session.execute(
        select(
            TokenUsageRow.resource_id,
            TokenUsageRow.deployment,
            func.min(TokenUsageRow.bucket_start),
        )
        .where(TokenUsageRow.tenant_id == tenant_id, TokenUsageRow.bucket_start >= since)
        .group_by(TokenUsageRow.resource_id, TokenUsageRow.deployment)
    )
    return {(r, d): _utc(m) for r, d, m in rows}


def _should_renotify(details: dict, ratio: Decimal | None, tokens: int) -> bool:
    """Ratio up by INTRADAY_RENOTIFY_GROWTH since last notified (tokens if no baseline)."""
    last_ratio = details.get("notified_ratio")
    if ratio is not None and last_ratio is not None:
        return ratio >= Decimal(last_ratio) * Decimal(str(INTRADAY_RENOTIFY_GROWTH))
    if ratio is None and last_ratio is None:
        last_tokens = int(details.get("notified_tokens") or 0)
        return tokens >= last_tokens * INTRADAY_RENOTIFY_GROWTH
    # Baseline appeared or vanished between runs: a different comparison, not "worse".
    return False


async def detect_intraday(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    """Raise/refresh/resolve "spike_intraday" alerts; returns alerts raised or refreshed."""
    hi = now.replace(minute=0, second=0, microsecond=0)
    lo = hi - timedelta(hours=INTRADAY_HOURS)
    day = (hi - timedelta(hours=1)).date()  # the newest complete hour's day
    current = await _window_tokens(session, tenant_id, lo, hi)
    history = [
        await _window_tokens(session, tenant_id, lo - timedelta(weeks=w), hi - timedelta(weeks=w))
        for w in range(1, BASELINE_WEEKS + 1)
    ]
    first_seen = await _first_seen(session, tenant_id, lo - timedelta(weeks=BASELINE_WEEKS))

    open_alerts = {
        a.subject: a
        for a in await session.scalars(
            select(AlertEvent).where(
                AlertEvent.tenant_id == tenant_id,
                AlertEvent.kind == KIND_INTRADAY,
                AlertEvent.resolved_at.is_(None),
            )
        )
    }
    subjects = {_subject(k, day) for k in current}
    alerts = await _alerts(session, tenant_id, KIND_INTRADAY, subjects)
    still_open: set[str] = set()
    raised = 0
    for key, tokens in sorted(current.items()):
        seen = first_seen.get(key)
        if seen is None or hi - seen < timedelta(days=MIN_HISTORY_DAYS):
            continue
        weeks = [
            Decimal(h.get(key, 0))
            for w, h in enumerate(history, start=1)
            if lo - timedelta(weeks=w) >= seen
        ]
        if not weeks:
            continue
        baseline = _median(weeks)
        value = Decimal(tokens)
        if not (value > baseline * SPIKE_RATIO and tokens >= INTRADAY_FLOOR_TOKENS):
            continue
        ratio = _ratio(value, baseline)
        subject = _subject(key, day)
        still_open.add(subject)
        resource, deployment = key
        label = queries.deployment_label(resource, deployment)
        times = f"{ratio}x" if ratio is not None else "up from nothing"
        message = (
            f"{label} used {tokens:,} tokens in the {INTRADAY_HOURS} hours to "
            f"{hi.strftime('%H:%M')} UTC, {times} the usual {int(baseline):,} for this time "
            "of the week"
        )
        alert = alerts.get(subject)
        if alert is not None and _manually_resolved(alert):
            continue
        if alert is None:
            alert = AlertEvent(tenant_id=tenant_id, kind=KIND_INTRADAY, subject=subject)
            session.add(alert)
            notify = True
        elif alert.notified_at is None:
            notify = True  # not sent yet: it will go out with the latest numbers
        elif _should_renotify(alert.details or {}, ratio, tokens):
            alert.notified_at = None
            notify = True
        else:
            notify = False
        previous = dict(alert.details or {})
        details = {
            "resource_id": resource,
            "deployment": deployment,
            "day": day.isoformat(),
            "window_start": lo.isoformat(),
            "window_end": hi.isoformat(),
            "tokens": tokens,
            "baseline_tokens": int(baseline),
            "baseline_weeks": len(weeks),
            "ratio": str(ratio) if ratio is not None else None,
            "notified_ratio": str(ratio) if ratio is not None else None,
            "notified_tokens": tokens,
            "peak_ratio": previous.get("peak_ratio"),
        }
        if not notify:
            details["notified_ratio"] = previous.get("notified_ratio")
            details["notified_tokens"] = previous.get("notified_tokens")
        if ratio is not None and (
            details["peak_ratio"] is None or ratio > Decimal(details["peak_ratio"])
        ):
            details["peak_ratio"] = str(ratio)
        alert.message = message
        alert.details = details
        alert.severity = _severity(ratio)
        alert.resolved_at = None
        raised += 1
    # The rate is back to normal, or the alert belongs to an earlier day: close it. A
    # re-spike later today reopens the same row without notifying again unless it is worse.
    for subject, alert in open_alerts.items():
        if subject not in still_open:
            alert.resolved_at = now
    await session.commit()
    return raised


async def run_intraday_spikes(
    session_factory: SessionFactory, settings: Settings, *, now: datetime | None = None
) -> int:
    """15-minute job: token rate spikes. Returns spike alerts raised or refreshed."""
    now = _now(now)
    async with session_factory() as session:
        tenant_id = await _tenant(session, settings)
        if tenant_id is None:
            return 0
        return await detect_intraday(session, tenant_id, now)
