"""Alerts inbox: everything the jobs raised (budgets, spikes, variance, access...), newest first.

Resolving by hand is audited and marks ``details["resolved_by"]`` so jobs that honour it
(spike detection) don't reopen an alert someone already dealt with.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, TenantId
from ai_cost_tracker.db.models import AlertEvent

router = APIRouter(prefix="/alerts", tags=["alerts"])

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite hands back naive datetimes for timezone-aware columns.
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _out(a: AlertEvent) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "severity": a.severity,
        "subject": a.subject,
        "message": a.message,
        "details": a.details or {},
        "created_at": _iso(a.created_at),
        "resolved_at": _iso(a.resolved_at),
        "notified_at": _iso(a.notified_at),
    }


@router.get("")
async def list_alerts(
    session: SessionDep,
    tenant_id: TenantId,
    _: Annotated[Principal, Depends(get_principal)],
    status: Annotated[Literal["open", "resolved", "all"], Query()] = "open",
    kind: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Open alerts by default: the inbox is a to-do list, history is one filter away."""
    q = select(AlertEvent).where(AlertEvent.tenant_id == tenant_id)
    if status == "open":
        q = q.where(AlertEvent.resolved_at.is_(None))
    elif status == "resolved":
        q = q.where(AlertEvent.resolved_at.is_not(None))
    if kind:
        q = q.where(AlertEvent.kind == kind)
    q = q.order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc()).limit(limit)
    return [_out(a) for a in await session.scalars(q)]


@router.post("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Annotated[Principal, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Close an alert. Resolving one that is already closed is a no-op (and not audited)."""
    alert = await session.scalar(
        select(AlertEvent).where(AlertEvent.tenant_id == tenant_id, AlertEvent.id == alert_id)
    )
    if alert is None:
        raise HTTPException(404, "Alert not found")
    if alert.resolved_at is None:
        alert.resolved_at = datetime.now(UTC)
        alert.details = {**(alert.details or {}), "resolved_by": principal.oid}
        audit.record(
            session,
            tenant_id,
            principal,
            "alert.resolve",
            target_type="alert_event",
            target_id=str(alert.id),
            details={"kind": alert.kind, "subject": alert.subject},
        )
        await session.commit()
    return _out(alert)
