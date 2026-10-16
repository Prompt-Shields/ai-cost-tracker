"""Write connector output into the database.

Every function pulls from the connector first and only then opens the write, so slow cloud
APIs never hold a transaction open. Cost and usage use replace semantics: delete the rows for
the account and range, then insert what the connector returned, in one transaction. A re-run
therefore leaves identical rows, and late billing corrections overwrite stale figures.

Cost meters the parser doesn't recognise are queued in ``UnmappedMeter`` for an admin rather
than dropped; the queue is recomputed from the stored cost lines, so it is idempotent too.
"""

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, insert, select, tuple_
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.connectors.base import Connector
from ai_cost_tracker.core.models import CostRecord, Deployment, TokenUsage
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DeploymentRow,
    Tenant,
    TokenUsageRow,
    UnmappedMeter,
)

logger = logging.getLogger(__name__)

_MONEY_PLACES = Decimal("0.0000000001")  # Numeric(20, 10) in db/models.py


async def get_or_create_tenant(
    session: AsyncSession, slug: str, name: str | None = None
) -> uuid.UUID:
    """Return the tenant id for ``slug``, creating the tenant on first use."""
    tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is not None:
        return tenant_id
    tenant = Tenant(slug=slug, name=name or slug)
    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError:
        # Another process created it between our select and insert.
        await session.rollback()
        return await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    return tenant.id


async def sync_inventory(
    session: AsyncSession, tenant_id: uuid.UUID, connector: Connector
) -> dict[str, list[Deployment]]:
    """Upsert accounts and deployments; soft-delete deployments the cloud no longer reports."""
    found: dict[str, list[Deployment]] = {}
    accounts = []
    for account in await connector.list_accounts():
        # One unreadable subscription (e.g. Reader removed) must not block the others; its
        # stored inventory is left untouched rather than marked deleted.
        try:
            found[account.account_id] = await connector.list_deployments(account.account_id)
        except Exception:
            logger.exception("listing deployments failed for account %s", account.account_id)
            continue
        accounts.append(account)

    now = datetime.now(UTC)
    try:
        for account in accounts:
            deployments = found[account.account_id]
            row = await session.scalar(
                select(CloudAccountRow).where(
                    CloudAccountRow.tenant_id == tenant_id,
                    CloudAccountRow.cloud == account.cloud,
                    CloudAccountRow.account_id == account.account_id,
                )
            )
            if row is None:
                row = CloudAccountRow(
                    tenant_id=tenant_id, cloud=account.cloud, account_id=account.account_id
                )
                session.add(row)
            # tracked / access_status / discovered_at belong to onboarding and the admin;
            # inventory never touches them, and new rows keep the model defaults.
            row.name = account.name
            row.has_ai_resources = account.has_ai_resources or bool(deployments)
            row.last_synced_at = now

            existing = {
                (d.resource_id, d.name): d
                for d in await session.scalars(
                    select(DeploymentRow).where(
                        DeploymentRow.tenant_id == tenant_id,
                        DeploymentRow.cloud == account.cloud,
                        DeploymentRow.account_id == account.account_id,
                    )
                )
            }
            seen: set[tuple[str, str]] = set()
            for dep in deployments:
                key = (dep.resource_id, dep.name)
                seen.add(key)
                dep_row = existing.get(key)
                if dep_row is None:
                    dep_row = DeploymentRow(
                        tenant_id=tenant_id,
                        cloud=dep.cloud,
                        resource_id=dep.resource_id,
                        name=dep.name,
                    )
                    session.add(dep_row)
                dep_row.account_id = dep.account_id
                dep_row.model = dep.model
                dep_row.model_version = dep.model_version
                dep_row.sku = dep.sku
                dep_row.capacity = dep.capacity
                dep_row.region = dep.region
                dep_row.tags = dict(dep.tags)
                dep_row.deleted_at = None
            for key, dep_row in existing.items():
                if key not in seen and dep_row.deleted_at is None:
                    dep_row.deleted_at = now
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return found


async def ingest_cost(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connector: Connector,
    account_id: str,
    start: date,
    end: date,
) -> int:
    """Replace the account's cost lines for ``start``..``end`` inclusive; return rows written."""
    records = await connector.pull_cost(account_id, start, end)
    rows = [_cost_row(tenant_id, r) for r in _merge_cost(records)]

    clouds = await _account_clouds(session, tenant_id, account_id, (r["cloud"] for r in rows))
    where = [
        CostLine.tenant_id == tenant_id,
        CostLine.sub_account_id == account_id,
        CostLine.charge_period_start >= start,
        CostLine.charge_period_start <= end,
    ]
    if clouds:
        where.append(CostLine.cloud.in_(clouds))
    try:
        # Unmapped meters whose lines we are about to delete may vanish from the queue.
        stale = await session.execute(
            select(CostLine.cloud, CostLine.sku_meter)
            .where(*where, CostLine.ai_model.is_(None))
            .distinct()
        )
        touched = {(c, m) for c, m in stale} | {(r["cloud"], r["sku_meter"]) for r in rows}
        await session.execute(delete(CostLine).where(*where))
        if rows:
            await session.execute(insert(CostLine), rows)
        await _sync_unmapped(session, tenant_id, touched)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return len(rows)


async def ingest_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connector: Connector,
    account_id: str,
    deployments: list[Deployment],
    start: datetime,
    end: datetime,
) -> int:
    """Replace the account's metrics-sourced token usage in ``[start, end)``; return rows written.

    Gateway-sourced rows are left alone: they come from a different pipeline.
    """
    start, end = _utc(start), _utc(end)
    usage = await connector.pull_usage(account_id, deployments, start, end)
    rows = [
        _usage_row(tenant_id, u) for u in _merge_usage(usage) if start <= _utc(u.bucket_start) < end
    ]

    clouds = await _account_clouds(session, tenant_id, account_id, (r["cloud"] for r in rows))
    stmt = delete(TokenUsageRow).where(
        TokenUsageRow.tenant_id == tenant_id,
        TokenUsageRow.account_id == account_id,
        TokenUsageRow.source == "metrics",
        TokenUsageRow.bucket_start >= start,
        TokenUsageRow.bucket_start < end,
    )
    if clouds:
        stmt = stmt.where(TokenUsageRow.cloud.in_(clouds))
    return await _replace(session, stmt, TokenUsageRow, rows)


async def _replace(
    session: AsyncSession, delete_stmt: Any, model: type, rows: list[dict[str, Any]]
) -> int:
    try:
        await session.execute(delete_stmt)
        if rows:
            await session.execute(insert(model), rows)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return len(rows)


async def _sync_unmapped(
    session: AsyncSession, tenant_id: uuid.UUID, meters: set[tuple[str, str]]
) -> None:
    """Bring the unmapped-meter queue in line with the stored cost lines for ``meters``.

    Totals are recomputed from all of the meter's cost lines (not added to), so re-ingesting a
    range leaves the queue unchanged. Meters that now parse, or have no lines left, leave it.
    """
    parse = _meter_parser()
    if parse is None or not meters:
        return  # Without a parser every meter would look unmapped; don't flood the queue.
    unmapped = sorted(k for k in meters if parse(k[1]) is None)
    if mapped := sorted(k for k in meters if parse(k[1]) is not None):
        await _delete_unmapped(session, tenant_id, mapped)
    if not unmapped:
        return

    stats = await session.execute(
        select(
            CostLine.cloud,
            CostLine.sku_meter,
            CostLine.billing_currency,
            func.min(CostLine.charge_period_start),
            func.max(CostLine.charge_period_start),
            func.sum(CostLine.billed_cost),
            func.count(),
        )
        .where(
            CostLine.tenant_id == tenant_id,
            tuple_(CostLine.cloud, CostLine.sku_meter).in_(unmapped),
        )
        .group_by(CostLine.cloud, CostLine.sku_meter, CostLine.billing_currency)
    )
    best: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for cloud, meter, currency, first, last, total, count in stats:
        # SQLite's SUM may come back as a float; go through str() and fix the scale.
        total = Decimal(str(total or 0)).quantize(_MONEY_PLACES)
        prev = best.get((cloud, meter))
        # One row per meter: if it is billed in several currencies (rare), keep the one with
        # the most lines rather than adding amounts in different currencies.
        if prev is None or (count, total) > (prev[0], prev[1]["total_cost"]):
            row = {
                "tenant_id": tenant_id,
                "cloud": cloud,
                "sku_meter": meter,
                "first_seen": first,
                "last_seen": last,
                "total_cost": total,
                "currency": currency,
            }
            best[(cloud, meter)] = (count, row)
    if gone := [k for k in unmapped if k not in best]:
        await _delete_unmapped(session, tenant_id, gone)
    if best:
        await _upsert_unmapped(session, [row for _, row in best.values()])


async def _delete_unmapped(
    session: AsyncSession, tenant_id: uuid.UUID, meters: list[tuple[str, str]]
) -> None:
    await session.execute(
        delete(UnmappedMeter).where(
            UnmappedMeter.tenant_id == tenant_id,
            tuple_(UnmappedMeter.cloud, UnmappedMeter.sku_meter).in_(meters),
        )
    )


async def _upsert_unmapped(session: AsyncSession, values: list[dict[str, Any]]) -> None:
    """Insert-or-update on the unique key, so two accounts' ingests can't race on a meter."""
    dialect = (await session.connection()).dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"unmapped-meter upsert not supported on {dialect}")
    module = postgresql if dialect == "postgresql" else sqlite
    stmt = module.insert(UnmappedMeter).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "cloud", "sku_meter"],
        set_={
            c: getattr(stmt.excluded, c)
            for c in ("first_seen", "last_seen", "total_cost", "currency")
        },
    )
    await session.execute(stmt)


async def _account_clouds(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: str, record_clouds: Iterable[str]
) -> list[str]:
    """Clouds the account belongs to, so a delete never reaches another cloud's same-named id.

    Empty when neither the inventory nor the pulled records tell us; the delete then matches
    on account id alone, which still replaces the account's stale rows.
    """
    known = await session.scalars(
        select(CloudAccountRow.cloud).where(
            CloudAccountRow.tenant_id == tenant_id, CloudAccountRow.account_id == account_id
        )
    )
    return sorted(set(known) | set(record_clouds))


def _merge_cost(records: list[CostRecord]) -> list[CostRecord]:
    """Sum records that collide on the table's unique key instead of failing the insert."""
    merged: dict[tuple, CostRecord] = {}
    for r in records:
        key = (r.cloud, r.sub_account_id, r.resource_id or "", r.sku_meter, r.charge_period_start)
        prev = merged.get(key)
        if prev is None:
            merged[key] = r
            continue
        qty = (
            None
            if prev.consumed_quantity is None and r.consumed_quantity is None
            else (prev.consumed_quantity or Decimal(0)) + (r.consumed_quantity or Decimal(0))
        )
        merged[key] = prev.model_copy(
            update={"billed_cost": prev.billed_cost + r.billed_cost, "consumed_quantity": qty}
        )
    return list(merged.values())


def _merge_usage(usage: list[TokenUsage]) -> list[TokenUsage]:
    merged: dict[tuple, TokenUsage] = {}
    for u in usage:
        key = (u.cloud, u.resource_id, u.deployment, _utc(u.bucket_start), u.source)
        prev = merged.get(key)
        if prev is None:
            merged[key] = u
            continue
        merged[key] = prev.model_copy(
            update={
                "input_tokens": prev.input_tokens + u.input_tokens,
                "output_tokens": prev.output_tokens + u.output_tokens,
                "requests": prev.requests + u.requests,
            }
        )
    return list(merged.values())


def _cost_row(tenant_id: uuid.UUID, r: CostRecord) -> dict[str, Any]:
    model, direction = _parse(r.sku_meter)
    return {
        "tenant_id": tenant_id,
        "cloud": r.cloud,
        "sub_account_id": r.sub_account_id,
        "resource_id": r.resource_id or "",
        "charge_period_start": r.charge_period_start,
        "service_name": r.service_name,
        "sku_meter": r.sku_meter,
        "consumed_quantity": r.consumed_quantity,
        "billed_cost": r.billed_cost,
        "billing_currency": r.billing_currency,
        "ai_model": model,
        "ai_direction": direction,
    }


def _usage_row(tenant_id: uuid.UUID, u: TokenUsage) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "cloud": u.cloud,
        "account_id": u.account_id,
        "resource_id": u.resource_id,
        "deployment": u.deployment,
        "model": u.model,
        "bucket_start": _utc(u.bucket_start),
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "requests": u.requests,
        "source": u.source,
    }


def _meter_parser() -> Any:
    """The current ``parse_meter``, looked up per call so rule changes and test patches apply."""
    try:
        from ai_cost_tracker.normalise.azure_meters import parse_meter
    except ImportError:
        return None
    return parse_meter


def _parse(meter: str) -> tuple[str | None, str | None]:
    """(model, direction) for a meter, or (None, None) when unmapped or no parser exists yet."""
    parse_meter = _meter_parser()
    if parse_meter is None:
        return None, None
    info = parse_meter(meter)
    if info is None:
        return None, None
    return info.model, info.direction


def _utc(dt: datetime) -> datetime:
    """Naive datetimes are taken as UTC; SQLite drops tzinfo, so compare in one zone."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
