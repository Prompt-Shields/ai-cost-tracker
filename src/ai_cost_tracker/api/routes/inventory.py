"""Cloud accounts and AI deployments the tracker knows about."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ai_cost_tracker.api.auth import get_principal
from ai_cost_tracker.api.deps import SessionDep, TenantId
from ai_cost_tracker.db.models import CloudAccountRow, DeploymentRow


def _utc_iso(value: datetime | None) -> str | None:
    """ISO-8601 in UTC. SQLite drops tzinfo on the way back; we only ever store UTC."""
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat()


router = APIRouter(tags=["inventory"], dependencies=[Depends(get_principal)])


@router.get("/accounts")
async def list_accounts(
    session: SessionDep,
    tenant_id: TenantId,
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(CloudAccountRow)
        .where(CloudAccountRow.tenant_id == tenant_id)
        .order_by(CloudAccountRow.cloud, CloudAccountRow.name, CloudAccountRow.account_id)
    )
    return [
        {
            "cloud": r.cloud,
            "account_id": r.account_id,
            "name": r.name,
            "has_ai_resources": r.has_ai_resources,
            "last_synced_at": _utc_iso(r.last_synced_at),
        }
        for r in rows
    ]


@router.get("/deployments")
async def list_deployments(
    session: SessionDep,
    tenant_id: TenantId,
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(DeploymentRow)
        .where(DeploymentRow.tenant_id == tenant_id, DeploymentRow.deleted_at.is_(None))
        .order_by(DeploymentRow.account_id, DeploymentRow.resource_id, DeploymentRow.name)
    )
    return [
        {
            "cloud": r.cloud,
            "account_id": r.account_id,
            "resource_id": r.resource_id,
            "name": r.name,
            "model": r.model,
            "model_version": r.model_version,
            "sku": r.sku,
            "capacity": r.capacity,
            "tags": r.tags or {},
        }
        for r in rows
    ]
