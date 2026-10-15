"""Connection health panel and the "new subscription found" banner (issues #24, #25)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, SettingsDep, TenantId
from ai_cost_tracker.db.models import CloudAccountRow, SyncState
from ai_cost_tracker.health import describe

router = APIRouter(tags=["connections"], dependencies=[Depends(get_principal)])

HEALTH_SOURCES = ("inventory", "cost", "usage")
BACKFILL_SOURCES = ("cost", "usage")


class TrackBody(BaseModel):
    tracked: bool


async def enqueue_backfill(account_id: str, cloud: str = "azure") -> None:
    """Queue a backfill. Imported lazily: worker.enqueue pulls in arq/Redis settings."""
    from ai_cost_tracker.worker.enqueue import enqueue_backfill as enqueue

    await enqueue(account_id, cloud)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat()


@router.get("/connections")
async def list_connections(
    session: SessionDep, tenant_id: TenantId, settings: SettingsDep
) -> list[dict[str, Any]]:
    accounts = (
        await session.scalars(
            select(CloudAccountRow)
            .where(CloudAccountRow.tenant_id == tenant_id)
            .order_by(CloudAccountRow.cloud, CloudAccountRow.name, CloudAccountRow.account_id)
        )
    ).all()
    states = await _sync_states(session, tenant_id)
    now = datetime.now(UTC)
    return [_connection(a, states, now, settings.azure_principal_id) for a in accounts]


@router.get("/connections/discovered")
async def list_discovered(session: SessionDep, tenant_id: TenantId) -> list[dict[str, Any]]:
    """Untracked accounts with AI resources: the "N new subscriptions found" banner."""
    rows = await session.scalars(
        select(CloudAccountRow)
        .where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.tracked.is_(False),
            CloudAccountRow.has_ai_resources.is_(True),
        )
        .order_by(CloudAccountRow.name, CloudAccountRow.account_id)
    )
    return [
        {"account_id": r.account_id, "name": r.name, "discovered_at": utc_iso(r.discovered_at)}
        for r in rows
    ]


@router.post("/connections/{account_id}/track")
async def track_account(
    account_id: str,
    body: TrackBody,
    session: SessionDep,
    tenant_id: TenantId,
    settings: SettingsDep,
    _: Annotated[Principal, Depends(require_role("admin"))],
    cloud: str = "azure",
) -> dict[str, Any]:
    account = await session.scalar(
        select(CloudAccountRow).where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.cloud == cloud,
            CloudAccountRow.account_id == account_id,
        )
    )
    if account is None:
        raise HTTPException(404, f"Unknown account {account_id}")
    account.tracked = body.tracked
    await session.commit()
    if body.tracked and account.access_status == "connected":
        await enqueue_backfill(account.account_id, account.cloud)
    states = await _sync_states(session, tenant_id, account_id)
    return _connection(account, states, datetime.now(UTC), settings.azure_principal_id)


async def _sync_states(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: str | None = None
) -> dict[tuple[str, str, str], SyncState]:
    query = select(SyncState).where(SyncState.tenant_id == tenant_id)
    if account_id is not None:
        query = query.where(SyncState.account_id == account_id)
    return {(s.cloud, s.account_id, s.source): s for s in await session.scalars(query)}


def _connection(
    account: CloudAccountRow,
    states: dict[tuple[str, str, str], SyncState],
    now: datetime,
    principal_id: str,
) -> dict[str, Any]:
    def state(source: str) -> SyncState | None:
        return states.get((account.cloud, account.account_id, source))

    sources = {}
    for source in HEALTH_SOURCES:
        h = describe(
            source,
            state(source),
            now,
            account.access_status,
            account_id=account.account_id,
            principal_id=principal_id,
        )
        sources[source] = {
            "status": h.status,
            "last_success_at": utc_iso(h.last_success_at),
            "message": h.message,
            "fix": h.fix,
        }
    return {
        "cloud": account.cloud,
        "account_id": account.account_id,
        "name": account.name,
        "tracked": account.tracked,
        "access_status": account.access_status,
        "sources": sources,
        "backfill": {s: _backfill(state(s)) for s in BACKFILL_SOURCES},
    }


def _backfill(row: SyncState | None) -> dict[str, Any] | None:
    if row is None or row.backfill_from is None:
        return None
    return {
        "progress": float(row.backfill_progress or 0),
        "from": row.backfill_from.isoformat(),
        "done_through": row.backfill_done_through.isoformat()
        if row.backfill_done_through
        else None,
    }
