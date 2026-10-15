"""arq job functions and the plain ingestion passes behind them.

Each pass takes ``(session_factory, connector, settings)`` so tests can run it against SQLite
with the FakeConnector. Every account gets its own session: one subscription that fails
(missing permission, throttling, bad data) is logged and skipped, and the rest still ingest.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import ingest
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors import Connector, get_connector
from ai_cost_tracker.core.models import Deployment
from ai_cost_tracker.db.models import CloudAccountRow, DeploymentRow
from ai_cost_tracker.db.session import get_sessionmaker

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# Metrics land within minutes, so re-read the last two hours plus the current partial hour.
USAGE_WINDOW = timedelta(hours=2)
# Cost Management data settles over a couple of days; re-read the last three.
COST_WINDOW_DAYS = 3


@dataclass
class PassResult:
    """Rows written and the accounts that failed in one pass."""

    rows: int = 0
    failed: list[str] = field(default_factory=list)

    def add(self, other: "PassResult") -> None:
        self.rows += other.rows
        self.failed.extend(a for a in other.failed if a not in self.failed)


def usage_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Hour-aligned ``[start, end)`` covering the last two hours and the current one."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    hour = now.replace(minute=0, second=0, microsecond=0)
    return hour - USAGE_WINDOW, hour + timedelta(hours=1)


def cost_window(today: date | None = None, days: int = COST_WINDOW_DAYS) -> tuple[date, date]:
    """Inclusive ``(start, end)`` of the last ``days`` days, ending today (UTC)."""
    end = today or datetime.now(UTC).date()
    return end - timedelta(days=days - 1), end


async def _tenant_id(session_factory: SessionFactory, settings: Settings) -> uuid.UUID:
    async with session_factory() as session:
        return await ingest.get_or_create_tenant(session, slug=settings.default_tenant_slug)


async def _known_inventory(
    session_factory: SessionFactory, tenant_id: uuid.UUID
) -> dict[str, list[Deployment]]:
    """Accounts and live deployments from the last inventory sync, without calling the cloud."""
    async with session_factory() as session:
        accounts = (
            await session.scalars(
                select(CloudAccountRow.account_id).where(CloudAccountRow.tenant_id == tenant_id)
            )
        ).all()
        rows = (
            await session.scalars(
                select(DeploymentRow).where(
                    DeploymentRow.tenant_id == tenant_id, DeploymentRow.deleted_at.is_(None)
                )
            )
        ).all()
    inventory: dict[str, list[Deployment]] = {a: [] for a in accounts}
    for r in rows:
        inventory.setdefault(r.account_id, []).append(
            Deployment(
                cloud=r.cloud,
                account_id=r.account_id,
                resource_id=r.resource_id,
                name=r.name,
                model=r.model,
                model_version=r.model_version,
                sku=r.sku,
                capacity=r.capacity,
                tags=r.tags or {},
            )
        )
    return inventory


async def run_inventory(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> dict[str, list[Deployment]]:
    tenant_id = await _tenant_id(session_factory, settings)
    async with session_factory() as session:
        inventory = await ingest.sync_inventory(session, tenant_id, connector)
    log.info("inventory: %d accounts, %d deployments", len(inventory), _count(inventory))
    return inventory


async def _inventory_or_sync(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> tuple[uuid.UUID, dict[str, list[Deployment]]]:
    """Use the stored inventory; sync once if nothing has been synced yet (fresh install)."""
    tenant_id = await _tenant_id(session_factory, settings)
    inventory = await _known_inventory(session_factory, tenant_id)
    if not inventory:
        inventory = await run_inventory(session_factory, connector, settings)
    return tenant_id, inventory


async def run_usage(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    start: datetime,
    end: datetime,
) -> PassResult:
    tenant_id, inventory = await _inventory_or_sync(session_factory, connector, settings)
    result = PassResult()
    for account_id, deployments in inventory.items():
        if not deployments:
            continue
        try:
            async with session_factory() as session:
                result.rows += await ingest.ingest_usage(
                    session, tenant_id, connector, account_id, deployments, start, end
                )
        except Exception:
            log.exception("usage ingest failed for account %s", account_id)
            result.failed.append(account_id)
    log.info("usage %s..%s: %d rows, failed=%s", start, end, result.rows, result.failed)
    return result


async def run_cost(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    start: date,
    end: date,
) -> PassResult:
    tenant_id, inventory = await _inventory_or_sync(session_factory, connector, settings)
    result = PassResult()
    for account_id in inventory:
        try:
            async with session_factory() as session:
                result.rows += await ingest.ingest_cost(
                    session, tenant_id, connector, account_id, start, end
                )
        except Exception:
            log.exception("cost ingest failed for account %s", account_id)
            result.failed.append(account_id)
    log.info("cost %s..%s: %d rows, failed=%s", start, end, result.rows, result.failed)
    return result


async def run_backfill(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    days: int,
    now: datetime | None = None,
) -> PassResult:
    """Inventory, then cost and hourly usage for the last ``days`` days (today included)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    await _try_inventory(session_factory, connector, settings)
    result = await run_cost(session_factory, connector, settings, *cost_window(now.date(), days))
    _, usage_end = usage_window(now)
    usage_start = datetime.combine(now.date() - timedelta(days=days - 1), datetime.min.time(), UTC)
    result.add(await run_usage(session_factory, connector, settings, usage_start, usage_end))
    return result


async def run_once(
    session_factory: SessionFactory,
    connector: Connector,
    settings: Settings,
    now: datetime | None = None,
) -> PassResult:
    """One scheduled-equivalent pass: inventory, recent cost, recent usage."""
    await _try_inventory(session_factory, connector, settings)
    result = await run_cost(session_factory, connector, settings, *cost_window(_date(now)))
    result.add(await run_usage(session_factory, connector, settings, *usage_window(now)))
    return result


async def _try_inventory(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> None:
    """A failed inventory refresh falls back to the stored inventory rather than ending the pass."""
    try:
        await run_inventory(session_factory, connector, settings)
    except Exception:
        log.exception("inventory sync failed; using the last stored inventory")


def _date(now: datetime | None) -> date | None:
    return now.astimezone(UTC).date() if now else None


def _count(inventory: dict[str, list[Deployment]]) -> int:
    return sum(len(d) for d in inventory.values())


def _deps() -> tuple[SessionFactory, Connector, Settings]:
    settings = get_settings()
    return get_sessionmaker(), get_connector(settings), settings


# arq entry points. Results are kept small and picklable.


async def sync_inventory_job(ctx: dict[str, Any]) -> dict[str, int]:
    inventory = await run_inventory(*_deps())
    return {"accounts": len(inventory), "deployments": _count(inventory)}


async def ingest_usage_job(ctx: dict[str, Any]) -> dict[str, Any]:
    result = await run_usage(*_deps(), *usage_window())
    return {"rows": result.rows, "failed": result.failed}


async def ingest_cost_job(ctx: dict[str, Any]) -> dict[str, Any]:
    result = await run_cost(*_deps(), *cost_window())
    return {"rows": result.rows, "failed": result.failed}


async def backfill_job(ctx: dict[str, Any], days: int) -> dict[str, Any]:
    result = await run_backfill(*_deps(), days)
    return {"rows": result.rows, "failed": result.failed}
