"""arq job functions and the plain ingestion passes behind them (ARCHITECTURE.md §5).

Each pass takes ``(session_factory, connector, settings)`` so tests can run it against SQLite
with the FakeConnector. Scheduled passes only touch accounts that an admin tracks and that
we can read (``tracked`` and ``access_status == "connected"``). Every account gets its own
session and its own ``sync_state`` record: one subscription that fails (missing permission,
throttling, bad data) is logged and recorded, and the rest still ingest.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import ingest, sync_state
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors import Connector, get_connector
from ai_cost_tracker.core.models import Deployment
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DeploymentRow,
    SyncState,
    TokenUsageRow,
)
from ai_cost_tracker.db.session import get_sessionmaker
from ai_cost_tracker.worker.enqueue import backfill_job_id

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
Sleep = Callable[[float], Awaitable[Any]]

# Without a watermark, re-read the last two hours plus the current partial hour.
USAGE_WINDOW = timedelta(hours=2)
# With a watermark, re-read one hour before it: metrics for an hour keep landing for minutes.
USAGE_OVERLAP = timedelta(hours=1)
# Longest gap a scheduled usage pass catches up on; longer outages need a backfill.
USAGE_MAX_CATCHUP = timedelta(days=7)
# CLI pass (``act ingest-once``): Cost Management data settles over a couple of days.
COST_WINDOW_DAYS = 3
# Scheduled cost passes spread subscriptions over this many seconds so we don't hit the
# Cost Management per-tenant QPU limit with every subscription at once (issue #2).
COST_JITTER_SECONDS = 900

USAGE_BACKFILL_DAYS = 30
USAGE_BACKFILL_CHUNK_DAYS = 7
COST_BACKFILL_MONTHS = 12

ACCOUNT_NOT_LISTED = "account or its deployments could not be listed; check the Reader role"


@dataclass
class PassResult:
    """Rows written and the accounts that failed in one pass."""

    rows: int = 0
    failed: list[str] = field(default_factory=list)

    def add(self, other: "PassResult") -> None:
        self.rows += other.rows
        self.failed.extend(a for a in other.failed if a not in self.failed)

    def fail(self, account_id: str) -> None:
        if account_id not in self.failed:
            self.failed.append(account_id)


@dataclass
class TrackedAccount:
    cloud: str
    account_id: str
    deployments: list[Deployment] = field(default_factory=list)


# --- windows ---------------------------------------------------------------------------


def _hour(now: datetime | None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return now.replace(minute=0, second=0, microsecond=0)


def usage_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Hour-aligned ``[start, end)`` covering the last two hours and the current one."""
    hour = _hour(now)
    return hour - USAGE_WINDOW, hour + timedelta(hours=1)


def scheduled_usage_window(
    watermark: datetime | None, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """``[start, end)`` from one hour before the watermark (or two hours back) to now."""
    hour = _hour(now)
    if watermark is None:
        start = hour - USAGE_WINDOW
    else:
        start = _utc(watermark).replace(minute=0, second=0, microsecond=0) - USAGE_OVERLAP
    start = min(max(start, hour - USAGE_MAX_CATCHUP), hour - USAGE_OVERLAP)
    return start, hour + timedelta(hours=1)


def cost_window(today: date | None = None, days: int = COST_WINDOW_DAYS) -> tuple[date, date]:
    """Inclusive ``(start, end)`` of the last ``days`` days, ending today (UTC)."""
    end = today or datetime.now(UTC).date()
    return end - timedelta(days=days - 1), end


def open_invoice_window(today: date | None = None) -> tuple[date, date]:
    """Previous and current calendar month up to today: the periods Azure still restates.

    Assumes invoice periods follow calendar months (true for pay-as-you-go and EA/MCA
    billing months; unverified for custom billing periods, issue #2).
    """
    today = today or datetime.now(UTC).date()
    return _add_months(today.replace(day=1), -1), today


def jitter_delay(account_id: str, max_seconds: int = COST_JITTER_SECONDS) -> int:
    """Stable 0..max_seconds offset per account, so each subscription keeps its slot."""
    if max_seconds <= 0:
        return 0
    digest = hashlib.sha256(account_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") % (max_seconds + 1)


# --- account lookups -------------------------------------------------------------------


async def _tenant_id(session_factory: SessionFactory, settings: Settings) -> uuid.UUID:
    async with session_factory() as session:
        return await ingest.get_or_create_tenant(session, slug=settings.default_tenant_slug)


def _tracked_filter(tenant_id: uuid.UUID) -> tuple:
    return (
        CloudAccountRow.tenant_id == tenant_id,
        CloudAccountRow.tracked.is_(True),
        CloudAccountRow.access_status == "connected",
    )


async def _tracked_accounts(
    session_factory: SessionFactory,
    tenant_id: uuid.UUID,
    cloud: str | None = None,
    account_id: str | None = None,
) -> list[TrackedAccount]:
    """Tracked, connected accounts and their live deployments, without calling the cloud."""
    where = list(_tracked_filter(tenant_id))
    if cloud is not None:
        where.append(CloudAccountRow.cloud == cloud)
    if account_id is not None:
        where.append(CloudAccountRow.account_id == account_id)
    async with session_factory() as session:
        accounts = (
            await session.execute(
                select(CloudAccountRow.cloud, CloudAccountRow.account_id)
                .where(*where)
                .order_by(CloudAccountRow.account_id)
            )
        ).all()
        found = {(c, a): TrackedAccount(c, a) for c, a in accounts}
        if not found:
            return []
        rows = await session.scalars(
            select(DeploymentRow).where(
                DeploymentRow.tenant_id == tenant_id,
                DeploymentRow.deleted_at.is_(None),
                DeploymentRow.account_id.in_([a for _, a in found]),
            )
        )
        for r in rows:
            acct = found.get((r.cloud, r.account_id))
            if acct is not None:
                acct.deployments.append(_deployment(r))
    return list(found.values())


def _deployment(r: DeploymentRow) -> Deployment:
    return Deployment(
        cloud=r.cloud,
        account_id=r.account_id,
        resource_id=r.resource_id,
        name=r.name,
        model=r.model,
        model_version=r.model_version,
        sku=r.sku,
        capacity=r.capacity,
        region=r.region,
        tags=r.tags or {},
    )


async def _any_account(session_factory: SessionFactory, tenant_id: uuid.UUID) -> bool:
    async with session_factory() as session:
        return (
            await session.scalar(
                select(CloudAccountRow.id).where(CloudAccountRow.tenant_id == tenant_id).limit(1)
            )
        ) is not None


async def _accounts_or_sync(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> tuple[uuid.UUID, list[TrackedAccount]]:
    """Use the stored inventory; sync once if nothing has been synced yet (fresh install)."""
    tenant_id = await _tenant_id(session_factory, settings)
    if not await _any_account(session_factory, tenant_id):
        await _try_inventory(session_factory, connector, settings)
    return tenant_id, await _tracked_accounts(session_factory, tenant_id)


# --- sync_state helpers ----------------------------------------------------------------


async def _state(
    session: AsyncSession, tenant_id: uuid.UUID, acct: TrackedAccount, source: str
) -> SyncState | None:
    return await session.scalar(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.cloud == acct.cloud,
            SyncState.account_id == acct.account_id,
            SyncState.source == source,
        )
    )


async def _record_ok(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    acct: TrackedAccount,
    source: str,
    watermark: datetime | None = None,
) -> None:
    """Record success; the watermark only moves forward (a backfill never rewinds it)."""
    state = await _state(session, tenant_id, acct, source)
    if watermark is not None and state is not None and state.watermark is not None:
        watermark = max(_utc(watermark), _utc(state.watermark))
    await sync_state.record(
        session, tenant_id, acct.cloud, acct.account_id, source, watermark=watermark
    )


async def _record_error(
    session_factory: SessionFactory,
    tenant_id: uuid.UUID,
    acct: TrackedAccount,
    source: str,
    error: BaseException | str,
) -> None:
    try:
        async with session_factory() as session:
            await sync_state.record(
                session, tenant_id, acct.cloud, acct.account_id, source, error=error
            )
    except Exception:
        log.exception("could not record %s failure for %s", source, acct.account_id)


async def _newest_bucket(
    session: AsyncSession, tenant_id: uuid.UUID, acct: TrackedAccount
) -> datetime | None:
    newest = await session.scalar(
        select(func.max(TokenUsageRow.bucket_start)).where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.cloud == acct.cloud,
            TokenUsageRow.account_id == acct.account_id,
            TokenUsageRow.source == "metrics",
        )
    )
    return _utc(newest) if newest is not None else None


async def _newest_cost_day(
    session: AsyncSession, tenant_id: uuid.UUID, acct: TrackedAccount
) -> datetime | None:
    newest = await session.scalar(
        select(func.max(CostLine.charge_period_start)).where(
            CostLine.tenant_id == tenant_id,
            CostLine.cloud == acct.cloud,
            CostLine.sub_account_id == acct.account_id,
        )
    )
    return datetime.combine(newest, time.min, UTC) if newest is not None else None


# --- inventory -------------------------------------------------------------------------


async def run_inventory(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> dict[str, list[Deployment]]:
    tenant_id = await _tenant_id(session_factory, settings)
    try:
        async with session_factory() as session:
            inventory = await ingest.sync_inventory(session, tenant_id, connector)
    except Exception as exc:
        for acct in await _tracked_accounts(session_factory, tenant_id):
            await _record_error(session_factory, tenant_id, acct, "inventory", exc)
        raise
    for acct in await _tracked_accounts(session_factory, tenant_id):
        if acct.account_id in inventory:
            async with session_factory() as session:
                await _record_ok(session, tenant_id, acct, "inventory")
        else:
            await _record_error(session_factory, tenant_id, acct, "inventory", ACCOUNT_NOT_LISTED)
    log.info("inventory: %d accounts, %d deployments", len(inventory), _count(inventory))
    return inventory


async def _try_inventory(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> None:
    """A failed inventory refresh falls back to the stored inventory rather than ending the pass."""
    try:
        await run_inventory(session_factory, connector, settings)
    except Exception:
        log.exception("inventory sync failed; using the last stored inventory")


# --- usage (issue #17) -----------------------------------------------------------------


async def _ingest_usage_window(
    session_factory: SessionFactory,
    connector: Connector,
    tenant_id: uuid.UUID,
    acct: TrackedAccount,
    start: datetime,
    end: datetime,
) -> int:
    async with session_factory() as session:
        return await ingest.ingest_usage(
            session, tenant_id, connector, acct.account_id, acct.deployments, start, end
        )


async def run_usage(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    now: datetime | None = None,
) -> PassResult:
    """Token usage per tracked account, then estimates for the days touched.

    With no explicit window, each account is read from its usage watermark (see
    ``scheduled_usage_window``). The new watermark is the newest bucket ingested, or the
    last bucket queried when the account was idle, so idle accounts don't widen the window.
    """
    tenant_id, accounts = await _accounts_or_sync(session_factory, connector, settings)
    result = PassResult()
    first_day = last_day = None
    for acct in accounts:
        try:
            async with session_factory() as session:
                state = await _state(session, tenant_id, acct, "usage")
            if start is not None and end is not None:
                a_start, a_end = start, end
            else:
                a_start, a_end = scheduled_usage_window(state.watermark if state else None, now)
            a_start, a_end = _utc(a_start), _utc(a_end)
            rows = 0
            if acct.deployments:
                rows = await _ingest_usage_window(
                    session_factory, connector, tenant_id, acct, a_start, a_end
                )
            async with session_factory() as session:
                newest = await _newest_bucket(session, tenant_id, acct)
                last_bucket = a_end - timedelta(hours=1)
                watermark = newest if newest is not None and newest >= a_start else last_bucket
                await _record_ok(session, tenant_id, acct, "usage", min(watermark, last_bucket))
            result.rows += rows
            first_day = min(first_day or a_start.date(), a_start.date())
            last_day = max(last_day or a_end.date(), (a_end - timedelta(microseconds=1)).date())
        except Exception as exc:
            log.exception("usage ingest failed for account %s", acct.account_id)
            result.fail(acct.account_id)
            await _record_error(session_factory, tenant_id, acct, "usage", exc)
    log.info("usage: %d rows, failed=%s", result.rows, result.failed)
    if first_day is not None:
        today = _hour(now).date()
        await run_estimates(
            session_factory,
            tenant_id,
            min(first_day, today - timedelta(days=1)),
            max(last_day or today, today),
        )
    return result


# --- cost (issue #18) ------------------------------------------------------------------


async def run_cost(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    start: date | None = None,
    end: date | None = None,
    *,
    today: date | None = None,
    jitter_seconds: int = 0,
    sleep: Sleep = asyncio.sleep,
) -> PassResult:
    """Billed cost per tracked account, then reconciliation for the same range.

    With no explicit range this is the scheduled pass: the open invoice periods (previous and
    current month). ``jitter_seconds`` spreads accounts over the run by a stable per-account
    delay; ``sleep`` is injectable so tests don't wait.
    """
    today = today or datetime.now(UTC).date()
    if start is None or end is None:
        start, end = open_invoice_window(today)
    tenant_id, accounts = await _accounts_or_sync(session_factory, connector, settings)
    result = PassResult()
    elapsed = 0
    for acct in sorted(
        accounts, key=lambda a: (jitter_delay(a.account_id, jitter_seconds), a.account_id)
    ):
        delay = jitter_delay(acct.account_id, jitter_seconds)
        if delay > elapsed:
            await sleep(delay - elapsed)
            elapsed = delay
        try:
            async with session_factory() as session:
                result.rows += await ingest.ingest_cost(
                    session, tenant_id, connector, acct.account_id, start, end
                )
            async with session_factory() as session:
                newest = await _newest_cost_day(session, tenant_id, acct)
                await _record_ok(session, tenant_id, acct, "cost", newest)
        except Exception as exc:
            log.exception("cost ingest failed for account %s", acct.account_id)
            result.fail(acct.account_id)
            await _record_error(session_factory, tenant_id, acct, "cost", exc)
    log.info("cost %s..%s: %d rows, failed=%s", start, end, result.rows, result.failed)
    if accounts:
        await run_reconcile(session_factory, tenant_id, start, end, today)
    return result


# --- estimates, reconciliation, prices, discovery (owned by other modules) -------------


async def run_estimates(
    session_factory: SessionFactory, tenant_id: uuid.UUID, start: date, end: date
) -> int | None:
    """Recompute token-based estimates. Ingested rows stay even if this step fails."""
    try:
        from ai_cost_tracker.estimate import compute_estimates
    except ImportError:
        log.warning("ai_cost_tracker.estimate not available; skipping estimates")
        return None
    try:
        async with session_factory() as session:
            return await compute_estimates(session, tenant_id, start, end)
    except Exception:
        log.exception("estimates failed for %s..%s", start, end)
        return None


async def run_reconcile(
    session_factory: SessionFactory,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    today: date | None = None,
) -> int | None:
    """Compare estimates with billed cost. Ingested rows stay even if this step fails."""
    try:
        from ai_cost_tracker.reconcile import reconcile
    except ImportError:
        log.warning("ai_cost_tracker.reconcile not available; skipping reconciliation")
        return None
    try:
        async with session_factory() as session:
            return await reconcile(session, tenant_id, start, end, today=today)
    except Exception:
        log.exception("reconciliation failed for %s..%s", start, end)
        return None


async def run_price_sync(session_factory: SessionFactory) -> int | None:
    try:
        from ai_cost_tracker.pricing.retail import sync_prices
    except ImportError:
        log.warning("ai_cost_tracker.pricing.retail not available; skipping price sync")
        return None
    async with session_factory() as session:
        count = await sync_prices(session)
    log.info("prices: %d rows", count)
    return count


async def run_discovery_pass(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> Any:
    try:
        from ai_cost_tracker.worker.discovery import run_discovery
    except ImportError:
        log.warning("ai_cost_tracker.worker.discovery not available; skipping discovery")
        return None
    return await run_discovery(session_factory, connector, settings)


async def run_access_check(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    enqueue: Callable[[str], Awaitable[Any]] | None = None,
) -> list[str]:
    """Re-check accounts waiting for Reader; start a backfill for each that just connected.

    ``enqueue`` hands the backfill to the queue (the worker passes arq's pool); without it
    the backfill runs inline, which is what the CLI and tests want.
    """
    try:
        from ai_cost_tracker.worker.discovery import check_pending_access
    except ImportError:
        log.warning("ai_cost_tracker.worker.discovery not available; skipping access check")
        return []
    connected = list(await check_pending_access(session_factory, connector, settings))
    for account_id in connected:
        try:
            if enqueue is not None:
                await enqueue(account_id)
            else:
                await run_account_backfill(session_factory, connector, settings, account_id)
        except Exception:
            log.exception("could not start backfill for %s", account_id)
    return connected


# --- backfill on connect ---------------------------------------------------------------


async def run_account_backfill(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    account_id: str,
    *,
    cloud: str = "azure",
    now: datetime | None = None,
) -> PassResult:
    """30 days of usage, then 12 months of cost newest-first, with progress in sync_state.

    Resumable: each source continues below its ``backfill_done_through``. Each chunk is a
    replace, so re-running a finished or half-finished backfill leaves identical rows.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    today = now.date()
    tenant_id = await _tenant_id(session_factory, settings)
    accounts = await _tracked_accounts(session_factory, tenant_id, cloud, account_id)
    if not accounts or not accounts[0].deployments:
        # Just connected: the hourly inventory may not have listed its deployments yet.
        await _try_inventory(session_factory, connector, settings)
        accounts = await _tracked_accounts(session_factory, tenant_id, cloud, account_id)
    if not accounts:
        log.warning("backfill skipped: %s/%s is not tracked and connected", cloud, account_id)
        return PassResult()
    acct = accounts[0]

    result = PassResult()
    usage_from = await _backfill_usage(session_factory, connector, tenant_id, acct, now, result)
    cost_from = await _backfill_cost(session_factory, connector, tenant_id, acct, today, result)
    await run_estimates(session_factory, tenant_id, usage_from, today)
    await run_reconcile(session_factory, tenant_id, cost_from, today, today)
    log.info("backfill %s: %d rows, failed=%s", account_id, result.rows, result.failed)
    return result


async def _done_through(
    session_factory: SessionFactory, tenant_id: uuid.UUID, acct: TrackedAccount, source: str
) -> date | None:
    async with session_factory() as session:
        state = await _state(session, tenant_id, acct, source)
    return state.backfill_done_through if state else None


async def _backfill_usage(
    session_factory: SessionFactory,
    connector: Connector,
    tenant_id: uuid.UUID,
    acct: TrackedAccount,
    now: datetime,
    result: PassResult,
) -> date:
    """Newest-first 7-day chunks back to 30 days ago; returns the first day of the range."""
    today = now.date()
    backfill_from = today - timedelta(days=USAGE_BACKFILL_DAYS - 1)
    done = await _done_through(session_factory, tenant_id, acct, "usage")
    if done is not None and done <= backfill_from:
        return backfill_from
    hi = done - timedelta(days=1) if done is not None else today
    end_cap = _hour(now) + timedelta(hours=1)
    while hi >= backfill_from:
        lo = max(backfill_from, hi - timedelta(days=USAGE_BACKFILL_CHUNK_DAYS - 1))
        start = datetime.combine(lo, time.min, UTC)
        end = min(datetime.combine(hi + timedelta(days=1), time.min, UTC), end_cap)
        try:
            if acct.deployments:
                result.rows += await _ingest_usage_window(
                    session_factory, connector, tenant_id, acct, start, end
                )
            async with session_factory() as session:
                await sync_state.record_backfill(
                    session,
                    tenant_id,
                    acct.cloud,
                    acct.account_id,
                    "usage",
                    backfill_from=backfill_from,
                    done_through=lo,
                    progress=((today - lo).days + 1) / USAGE_BACKFILL_DAYS,
                )
        except Exception as exc:
            log.exception("usage backfill failed for %s at %s..%s", acct.account_id, lo, hi)
            result.fail(acct.account_id)
            await _record_error(session_factory, tenant_id, acct, "usage", exc)
            return backfill_from
        hi = lo - timedelta(days=1)
    async with session_factory() as session:
        await _record_ok(
            session, tenant_id, acct, "usage", await _newest_bucket(session, tenant_id, acct)
        )
    return backfill_from


async def _backfill_cost(
    session_factory: SessionFactory,
    connector: Connector,
    tenant_id: uuid.UUID,
    acct: TrackedAccount,
    today: date,
    result: PassResult,
) -> date:
    """One calendar month at a time, newest first; returns the first day of the range.

    Throttling (429 and Cost Management's retry-after headers) is retried inside the Azure
    client; a month that still fails stops the backfill so the next run resumes there.
    """
    months = [_add_months(today.replace(day=1), -i) for i in range(COST_BACKFILL_MONTHS)]
    backfill_from = months[-1]
    done = await _done_through(session_factory, tenant_id, acct, "cost")
    if done is not None and done <= backfill_from:
        return backfill_from
    for i, month in enumerate(months):
        if done is not None and month >= done:
            continue
        end = min(_add_months(month, 1) - timedelta(days=1), today)
        try:
            async with session_factory() as session:
                result.rows += await ingest.ingest_cost(
                    session, tenant_id, connector, acct.account_id, month, end
                )
            async with session_factory() as session:
                await sync_state.record_backfill(
                    session,
                    tenant_id,
                    acct.cloud,
                    acct.account_id,
                    "cost",
                    backfill_from=backfill_from,
                    done_through=month,
                    progress=(i + 1) / COST_BACKFILL_MONTHS,
                )
        except Exception as exc:
            log.exception("cost backfill failed for %s, month %s", acct.account_id, month)
            result.fail(acct.account_id)
            await _record_error(session_factory, tenant_id, acct, "cost", exc)
            return backfill_from
    async with session_factory() as session:
        await _record_ok(
            session, tenant_id, acct, "cost", await _newest_cost_day(session, tenant_id, acct)
        )
    return backfill_from


# --- CLI passes ------------------------------------------------------------------------


async def run_backfill(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    days: int,
    now: datetime | None = None,
) -> PassResult:
    """Inventory, hourly usage, estimates, then cost for the last ``days`` days (``act seed-demo``).

    Usage and estimates come first so the reconciliation that follows the cost pass compares
    billed cost against complete estimates.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    await _try_inventory(session_factory, connector, settings)
    _, usage_end = usage_window(now)
    usage_start = datetime.combine(now.date() - timedelta(days=days - 1), time.min, UTC)
    result = await run_usage(session_factory, connector, settings, usage_start, usage_end, now=now)
    cost_start, cost_end = cost_window(now.date(), days)
    await run_estimates(
        session_factory, await _tenant_id(session_factory, settings), cost_start, cost_end
    )
    result.add(
        await run_cost(session_factory, connector, settings, cost_start, cost_end, today=now.date())
    )
    return result


async def run_once(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    now: datetime | None = None,
) -> PassResult:
    """One pass for ``act ingest-once``: inventory, recent cost, recent usage."""
    today = _date(now)
    await _try_inventory(session_factory, connector, settings)
    # Usage (and its estimates) before cost, so reconciliation sees today's estimates.
    result = await run_usage(session_factory, connector, settings, *usage_window(now), now=now)
    result.add(
        await run_cost(session_factory, connector, settings, *cost_window(today), today=today)
    )
    return result


# --- small helpers ---------------------------------------------------------------------


def _add_months(day: date, months: int) -> date:
    """First-of-month arithmetic; ``day`` must be the 1st."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def _utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; naive datetimes are UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _date(now: datetime | None) -> date | None:
    return now.astimezone(UTC).date() if now else None


def _count(inventory: dict[str, list[Deployment]]) -> int:
    return sum(len(d) for d in inventory.values())


def _deps() -> tuple[SessionFactory, Connector, Settings]:
    settings = get_settings()
    return get_sessionmaker(), get_connector(settings), settings


# --- arq entry points. Results are kept small and picklable. ---------------------------


async def sync_inventory_job(ctx: dict[str, Any]) -> dict[str, int]:
    inventory = await run_inventory(*_deps())
    return {"accounts": len(inventory), "deployments": _count(inventory)}


async def ingest_usage_job(ctx: dict[str, Any]) -> dict[str, Any]:
    result = await run_usage(*_deps())
    return {"rows": result.rows, "failed": result.failed}


async def ingest_cost_job(ctx: dict[str, Any]) -> dict[str, Any]:
    result = await run_cost(*_deps(), jitter_seconds=COST_JITTER_SECONDS)
    return {"rows": result.rows, "failed": result.failed}


async def backfill_job(ctx: dict[str, Any], days: int) -> dict[str, Any]:
    result = await run_backfill(*_deps(), days)
    return {"rows": result.rows, "failed": result.failed}


async def backfill_account_job(
    ctx: dict[str, Any], account_id: str, cloud: str = "azure"
) -> dict[str, Any]:
    result = await run_account_backfill(*_deps(), account_id, cloud=cloud)
    return {"rows": result.rows, "failed": result.failed}


async def sync_prices_job(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"prices": await run_price_sync(get_sessionmaker())}


async def discovery_job(ctx: dict[str, Any]) -> dict[str, Any]:
    await run_discovery_pass(*_deps())
    return {}


async def check_access_job(ctx: dict[str, Any]) -> dict[str, Any]:
    redis = ctx.get("redis")

    async def enqueue(account_id: str) -> None:
        await redis.enqueue_job(
            "backfill_account_job",
            account_id,
            "azure",
            _job_id=backfill_job_id("azure", account_id),
        )

    connected = await run_access_check(*_deps(), enqueue=enqueue if redis is not None else None)
    return {"connected": connected}
