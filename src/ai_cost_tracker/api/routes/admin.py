"""Admin-only views: the queue of billing meters the parser doesn't recognise yet."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ai_cost_tracker.api.auth import require_role
from ai_cost_tracker.api.deps import SessionDep, TenantId, money
from ai_cost_tracker.db.models import UnmappedMeter

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.get("/unmapped-meters")
async def list_unmapped_meters(session: SessionDep, tenant_id: TenantId) -> list[dict[str, Any]]:
    """Biggest spend first: those are the mappings worth adding to the rules file next."""
    rows = await session.scalars(
        select(UnmappedMeter)
        .where(UnmappedMeter.tenant_id == tenant_id)
        .order_by(UnmappedMeter.total_cost.desc(), UnmappedMeter.sku_meter)
    )
    return [
        {
            "sku_meter": r.sku_meter,
            "first_seen": r.first_seen.isoformat(),
            "last_seen": r.last_seen.isoformat(),
            "total_cost": money(r.total_cost),
            "currency": r.currency,
        }
        for r in rows
    ]
