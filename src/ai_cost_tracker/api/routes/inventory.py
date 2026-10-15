"""Cloud accounts and AI deployments the tracker knows about, and per-deployment cost."""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, select

from ai_cost_tracker.api import queries as q
from ai_cost_tracker.api.auth import get_principal
from ai_cost_tracker.api.deps import Range, SessionDep, TenantId, money
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


def _deployments_query(tenant_id: uuid.UUID):
    """Deployments with their account's display name (null when the account isn't known)."""
    return (
        select(DeploymentRow, CloudAccountRow.name)
        .outerjoin(
            CloudAccountRow,
            and_(
                CloudAccountRow.tenant_id == DeploymentRow.tenant_id,
                CloudAccountRow.cloud == DeploymentRow.cloud,
                CloudAccountRow.account_id == DeploymentRow.account_id,
            ),
        )
        .where(DeploymentRow.tenant_id == tenant_id)
    )


def _deployment_json(r: DeploymentRow, account_name: str | None) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "cloud": r.cloud,
        "account_id": r.account_id,
        "account_name": account_name,
        "resource_id": r.resource_id,
        "name": r.name,
        "model": r.model,
        "model_version": r.model_version,
        "sku": r.sku,
        "capacity": r.capacity,
        "region": r.region,
        "tags": r.tags or {},
    }


@router.get("/deployments")
async def list_deployments(
    session: SessionDep,
    tenant_id: TenantId,
) -> list[dict[str, Any]]:
    result = await session.execute(
        _deployments_query(tenant_id)
        .where(DeploymentRow.deleted_at.is_(None))
        .order_by(DeploymentRow.account_id, DeploymentRow.resource_id, DeploymentRow.name)
    )
    return [_deployment_json(r, name) for r, name in result]


@router.get("/deployments/{deployment_id}/daily")
async def deployment_daily(
    deployment_id: uuid.UUID,
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
) -> dict[str, Any]:
    """One deployment's cost and tokens per day. Deleted deployments still answer (history).

    Billed cost is the deployment's share of its resource's bill (``api.queries``, §4.4).
    """
    found = (
        await session.execute(
            _deployments_query(tenant_id).where(DeploymentRow.id == deployment_id)
        )
    ).first()
    if found is None:
        raise HTTPException(404, "deployment not found")
    dep, account_name = found
    inventory = await q.load_inventory(session, tenant_id)
    filters = q.Filters(resource_id=dep.resource_id, deployment=dep.name)
    data = await q.load(session, tenant_id, rng.start, rng.end, filters, inventory)

    days: dict[date, dict[str, Any]] = defaultdict(
        lambda: {"num": [Decimal(0)] * 3, "tok": [0, 0], "statuses": set()}
    )
    for key, cell in data.cells.items():
        d = days[key.day]
        for i, v in enumerate((cell.billed, cell.estimated, data.effective(key, cell)[0])):
            d["num"][i] += v
        d["tok"][0] += cell.input_tokens
        d["tok"][1] += cell.output_tokens
        d["statuses"].add(data.day_status(key.day, key.account_id))
    rows = []
    for day, d in sorted(days.items()):
        billed, estimated, eff = d["num"]
        inp, out = d["tok"]
        rows.append(
            {
                "day": day.isoformat(),
                "billed_cost": money(billed),
                "estimated_cost": money(estimated),
                "input_tokens": inp,
                "output_tokens": out,
                "cost_per_1m_tokens": q.per_1m(eff, inp + out),
                "day_status": q.min_status(d["statuses"]),
            }
        )
    return {"deployment": _deployment_json(dep, account_name), "rows": rows}
