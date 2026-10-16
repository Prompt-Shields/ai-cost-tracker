"""Admin view of the audit log (ARCHITECTURE.md §7, issue #37).

Newest first, keyset-paginated on ``id`` (``before_id``) so pages stay stable while new rows
are written.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select

from ai_cost_tracker.api.auth import require_role
from ai_cost_tracker.api.deps import SessionDep, TenantId
from ai_cost_tracker.db.models import AuditLog

router = APIRouter(tags=["audit"], dependencies=[Depends(require_role("admin"))])

MAX_LIMIT = 500


@router.get("/audit")
async def list_audit(
    session: SessionDep,
    tenant_id: TenantId,
    action: Annotated[
        str | None, Query(description='Exact action, or a prefix ending in "." e.g. "budget."')
    ] = None,
    actor: Annotated[str | None, Query(description="Actor id, or part of the actor name")] = None,
    start: Annotated[date | None, Query(description="First day (UTC), inclusive")] = None,
    end: Annotated[date | None, Query(description="Last day (UTC), inclusive")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
    before_id: Annotated[int | None, Query(description="Return rows older than this id")] = None,
) -> dict[str, Any]:
    if start and end and start > end:
        raise HTTPException(422, "start must be on or before end")
    query = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if action:
        if action.endswith("."):
            query = query.where(AuditLog.action.startswith(action, autoescape=True))
        else:
            query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(
            or_(AuditLog.actor_id == actor, AuditLog.actor_name.icontains(actor, autoescape=True))
        )
    if start:
        query = query.where(AuditLog.at >= datetime.combine(start, time(), UTC))
    if end:
        query = query.where(AuditLog.at < datetime.combine(end + timedelta(days=1), time(), UTC))
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    rows = (await session.scalars(query.order_by(AuditLog.id.desc()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": [_row(r) for r in rows],
        "next_before_id": str(rows[-1].id) if more else None,
    }


def _row(r: AuditLog) -> dict[str, Any]:
    at = r.at.replace(tzinfo=UTC) if r.at.tzinfo is None else r.at.astimezone(UTC)
    return {
        "id": str(r.id),
        "at": at.isoformat(),
        "actor_id": r.actor_id,
        "actor_name": r.actor_name,
        "action": r.action,
        "target_type": r.target_type,
        "target_id": r.target_id,
        "details": r.details or {},
    }
