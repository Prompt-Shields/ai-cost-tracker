"""Daily discovery and access checks (ARCHITECTURE.md §4.3, "Staying connected").

``run_discovery`` finds subscriptions the managed identity can see that hold AI resources but
that nobody has added yet, and raises a "new subscription" banner. ``check_pending_access``
connects accounts whose Reader role has just been granted, and flags tracked accounts whose
role was removed. Both take ``(session_factory, connector, settings)`` like the passes in
``jobs.py``; the worker schedule calls them.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import ingest, sync_state
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.base import Connector
from ai_cost_tracker.db.models import AlertEvent, CloudAccountRow

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

CONNECTED = "connected"
WAITING = "waiting_for_access"
NO_ACCESS = "no_access"

NEW_SUBSCRIPTION = "new_subscription"
ACCESS_LOST = "access_lost"


async def run_discovery(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> list[str]:
    """Add newly visible accounts with AI resources as untracked; return their ids.

    New accounts are stored ``tracked=False`` so nothing is ingested until an admin opts in;
    an untracked account seen again only has its name and AI flag refreshed.
    """
    now = datetime.now(UTC)
    visible = [a for a in await connector.list_accounts(include_all=True) if a.has_ai_resources]
    new_ids: list[str] = []
    async with session_factory() as session:
        tenant_id = await ingest.get_or_create_tenant(session, settings.default_tenant_slug)
        for account in visible:
            row = await _account_row(session, tenant_id, account.cloud, account.account_id)
            if row is None:
                session.add(
                    CloudAccountRow(
                        tenant_id=tenant_id,
                        cloud=account.cloud,
                        account_id=account.account_id,
                        name=account.name,
                        has_ai_resources=True,
                        tracked=False,
                        access_status=CONNECTED,
                        discovered_at=now,
                    )
                )
                await _raise_alert(
                    session,
                    tenant_id,
                    NEW_SUBSCRIPTION,
                    account.account_id,
                    f"New subscription with AI resources found: {account.name}",
                    {"cloud": account.cloud, "name": account.name},
                )
                new_ids.append(account.account_id)
            elif not row.tracked:
                row.name = account.name
                row.has_ai_resources = True
                row.discovered_at = row.discovered_at or now
            else:
                # Tracked now, so the "new subscription" banner has done its job.
                await _resolve_alert(session, tenant_id, NEW_SUBSCRIPTION, row.account_id, now)
        await session.commit()
    log.info("discovery: %d visible with AI resources, new=%s", len(visible), new_ids)
    return new_ids


async def check_pending_access(
    session_factory: SessionFactory, connector: Connector, settings: Settings
) -> list[str]:
    """Re-check access; return ids of accounts that just became connected.

    - waiting_for_access -> connected once the role shows up.
    - connected and tracked -> no_access when the role is gone (raises an "access_lost"
      alert); no_access -> connected when it returns (resolves the alert).
    Returned ids include both waiting->connected and no_access->connected, so the caller can
    backfill them. An error other than forbidden/not-found leaves the status unchanged.
    """
    async with session_factory() as session:
        tenant_id = await ingest.get_or_create_tenant(session, settings.default_tenant_slug)
        rows = (
            await session.scalars(
                select(CloudAccountRow)
                .where(CloudAccountRow.tenant_id == tenant_id)
                .order_by(CloudAccountRow.account_id)
            )
        ).all()
    to_check = [
        (r.cloud, r.account_id, r.access_status)
        for r in rows
        if r.access_status == WAITING or (r.tracked and r.access_status in (CONNECTED, NO_ACCESS))
    ]
    connected: list[str] = []
    for cloud, account_id, status in to_check:
        # One session per account: a failure on one never blocks the others.
        async with session_factory() as session:
            if await _check_one(session, connector, tenant_id, cloud, account_id, status):
                connected.append(account_id)
    log.info("access check: %d accounts checked, connected=%s", len(to_check), connected)
    return connected


async def _check_one(
    session: AsyncSession,
    connector: Connector,
    tenant_id: uuid.UUID,
    cloud: str,
    account_id: str,
    status: str,
) -> bool:
    """Check one account and apply the transition. True if it just became connected."""
    try:
        ok = await connector.check_access(account_id)
    except Exception as exc:
        log.exception("access check failed for account %s", account_id)
        await session.rollback()
        await sync_state.record(session, tenant_id, cloud, account_id, "access", error=exc)
        return False

    now = datetime.now(UTC)
    row = await _account_row(session, tenant_id, cloud, account_id)
    if row is None:  # Deleted between listing and checking.
        return False
    if ok:
        row.access_status = CONNECTED
        await _resolve_alert(session, tenant_id, ACCESS_LOST, account_id, now)
        await session.commit()
        await sync_state.record(session, tenant_id, cloud, account_id, "access", now=now)
        return status != CONNECTED

    if status == WAITING:
        error = "Reader role not granted yet"
    else:
        error = "Reader role missing: the app can no longer read this subscription"
        if status == CONNECTED:
            row.access_status = NO_ACCESS
            await _raise_alert(
                session,
                tenant_id,
                ACCESS_LOST,
                account_id,
                f"Lost access to {row.name}: re-grant the Reader role",
                {"cloud": cloud, "name": row.name},
            )
    await session.commit()
    await sync_state.record(session, tenant_id, cloud, account_id, "access", error=error, now=now)
    return False


async def _account_row(
    session: AsyncSession, tenant_id: uuid.UUID, cloud: str, account_id: str
) -> CloudAccountRow | None:
    return await session.scalar(
        select(CloudAccountRow).where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.cloud == cloud,
            CloudAccountRow.account_id == account_id,
        )
    )


async def _alert(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, subject: str
) -> AlertEvent | None:
    return await session.scalar(
        select(AlertEvent).where(
            AlertEvent.tenant_id == tenant_id,
            AlertEvent.kind == kind,
            AlertEvent.subject == subject,
        )
    )


async def _raise_alert(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: str,
    subject: str,
    message: str,
    details: dict,
) -> None:
    """Upsert one open alert per (kind, subject); re-raising reopens a resolved one."""
    alert = await _alert(session, tenant_id, kind, subject)
    if alert is None:
        alert = AlertEvent(tenant_id=tenant_id, kind=kind, subject=subject)
        session.add(alert)
    alert.message = message
    alert.details = details
    alert.resolved_at = None


async def _resolve_alert(
    session: AsyncSession, tenant_id: uuid.UUID, kind: str, subject: str, now: datetime
) -> None:
    alert = await _alert(session, tenant_id, kind, subject)
    if alert is not None and alert.resolved_at is None:
        alert.resolved_at = now
