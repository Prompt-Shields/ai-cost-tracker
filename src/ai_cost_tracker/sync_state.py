"""Record per-account sync health (ARCHITECTURE.md §4.3, "Connection health").

Every job that talks to a cloud records its outcome here; the health panel reads it.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import SyncState

SOURCES = ("inventory", "cost", "usage", "access")


async def _row(
    session: AsyncSession, tenant_id: uuid.UUID, cloud: str, account_id: str, source: str
) -> SyncState:
    row = await session.scalar(
        select(SyncState).where(
            SyncState.tenant_id == tenant_id,
            SyncState.cloud == cloud,
            SyncState.account_id == account_id,
            SyncState.source == source,
        )
    )
    if row is None:
        row = SyncState(tenant_id=tenant_id, cloud=cloud, account_id=account_id, source=source)
        session.add(row)
    return row


async def record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    cloud: str,
    account_id: str,
    source: str,
    *,
    error: BaseException | str | None = None,
    watermark: datetime | None = None,
    now: datetime | None = None,
) -> None:
    """Record one attempt. ``error=None`` means success. Commits."""
    now = now or datetime.now(UTC)
    row = await _row(session, tenant_id, cloud, account_id, source)
    row.last_attempt_at = now
    if error is None:
        row.last_success_at = now
        row.last_error = None
        if watermark is not None:
            row.watermark = watermark
    else:
        row.last_error = (str(error) or type(error).__name__)[:2000]
    await session.commit()


async def record_backfill(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    cloud: str,
    account_id: str,
    source: str,
    *,
    backfill_from: date,
    done_through: date | None,
    progress: float,
) -> None:
    """Update backfill progress (0..1). ``done_through`` is the oldest day finished. Commits."""
    row = await _row(session, tenant_id, cloud, account_id, source)
    row.backfill_from = backfill_from
    row.backfill_done_through = done_through
    row.backfill_progress = Decimal(str(round(max(0.0, min(progress, 1.0)), 4)))
    await session.commit()
