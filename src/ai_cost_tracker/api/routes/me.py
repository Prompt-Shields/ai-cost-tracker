"""The signed-in principal, plus the "login" audit row (issue #37).

The API never sees the Entra sign-in itself (the web app signs in with MSAL), so "login" in
the audit log means *first API use per hour*: the web app calls ``GET /me`` at startup and we
record a "login" row unless the same principal already has one in the last hour.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import NO_TENANT, SessionDep, TenantId
from ai_cost_tracker.db.models import AuditLog

router = APIRouter(tags=["me"])

LOGIN_INTERVAL = timedelta(hours=1)


@router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(get_principal)],
    session: SessionDep,
    tenant_id: TenantId,
) -> Principal:
    if tenant_id != NO_TENANT:
        await _record_login(session, tenant_id, principal)
    return principal


async def _record_login(session: AsyncSession, tenant_id: uuid.UUID, principal: Principal) -> None:
    now = datetime.now(UTC)
    last = await session.scalar(
        select(AuditLog.at)
        .where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.actor_id == principal.oid,
            AuditLog.action == "login",
        )
        .order_by(AuditLog.at.desc())
        .limit(1)
    )
    if last is not None:
        last = last.replace(tzinfo=UTC) if last.tzinfo is None else last
        if now - last < LOGIN_INTERVAL:
            return
    audit.record(
        session,
        tenant_id,
        principal,
        "login",
        target_type="principal",
        target_id=principal.oid,
        details={"roles": sorted(principal.roles)},
    )
    await session.commit()
