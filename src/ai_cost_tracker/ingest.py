"""Write connector output into the database.

Every function pulls from the connector first and only then opens the write, so slow cloud
APIs never hold a transaction open. Cost and usage use replace semantics: delete the rows for
the account and range, then insert what the connector returned, in one transaction. A re-run
therefore leaves identical rows, and late billing corrections overwrite stale figures.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, select
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
)


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
    accounts = await connector.list_accounts()
    found: dict[str, list[Deployment]] = {}
    for account in accounts:
        found[account.account_id] = await connector.list_deployments(account.account_id)

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
    stmt = delete(CostLine).where(
        CostLine.tenant_id == tenant_id,
        CostLine.sub_account_id == account_id,
        CostLine.charge_period_start >= start,
        CostLine.charge_period_start <= end,
    )
    if clouds:
        stmt = stmt.where(CostLine.cloud.in_(clouds))
    return await _replace(session, stmt, CostLine, rows)


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


def _parse(meter: str) -> tuple[str | None, str | None]:
    """(model, direction) for a meter, or (None, None) when unmapped or no parser exists yet."""
    try:
        from ai_cost_tracker.normalise.azure_meters import parse_meter
    except ImportError:
        return None, None
    info = parse_meter(meter)
    if info is None:
        return None, None
    return info.model, info.direction


def _utc(dt: datetime) -> datetime:
    """Naive datetimes are taken as UTC; SQLite drops tzinfo, so compare in one zone."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
