"""Notification channels and delivery log (issue #30).

Targets (webhook URLs, addresses) are secrets in practice, so responses only ever carry a
masked form, and a webhook's signing secret is returned once, when it is generated.
"""

import secrets
import uuid
from datetime import UTC
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import NO_TENANT, SessionDep, SettingsDep, TenantId
from ai_cost_tracker.db.models import NotificationChannel, NotificationDelivery
from ai_cost_tracker.ingest import get_or_create_tenant
from ai_cost_tracker.notify import NotifyError, send_test
from ai_cost_tracker.notify.channels import mask_target, validate_target

router = APIRouter(prefix="/notifications", tags=["notifications"])

Kind = Literal["email", "slack", "teams", "webhook"]
Severity = Literal["info", "warning", "critical"]
Admin = Annotated[Principal, Depends(require_role("admin"))]
Reader = Annotated[Principal, Depends(get_principal)]


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Kind
    target: str = Field(min_length=1, max_length=2000)
    alert_kinds: list[str] = []
    min_severity: Severity = "warning"
    digest: bool = False
    active: bool = True
    signing_secret: str | None = Field(default=None, min_length=16, max_length=200)


class ChannelUpdate(BaseModel):
    """Partial update: omitted fields keep their value (clients never see the full target)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Kind | None = None
    target: str | None = Field(default=None, min_length=1, max_length=2000)
    alert_kinds: list[str] | None = None
    min_severity: Severity | None = None
    digest: bool | None = None
    active: bool | None = None
    signing_secret: str | None = Field(default=None, min_length=16, max_length=200)


def _out(c: NotificationChannel) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "kind": c.kind,
        "target_masked": mask_target(c.kind, c.target),
        "alert_kinds": list(c.alert_kinds or []),
        "min_severity": c.min_severity,
        "digest": c.digest,
        "active": c.active,
    }


def _audit_details(c: NotificationChannel) -> dict[str, Any]:
    """Safe to log: no target, no secret."""
    return {
        "name": c.name,
        "kind": c.kind,
        "alert_kinds": list(c.alert_kinds or []),
        "min_severity": c.min_severity,
        "digest": c.digest,
        "active": c.active,
    }


def _clean_target(kind: str, target: str, environment: str) -> str:
    try:
        return validate_target(kind, target, environment=environment)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _clean_kinds(kinds: list[str]) -> list[str]:
    return sorted({k.strip() for k in kinds if k.strip()})


async def _get(session: SessionDep, tenant_id: uuid.UUID, channel_id: uuid.UUID):
    channel = await session.scalar(
        select(NotificationChannel).where(
            NotificationChannel.tenant_id == tenant_id, NotificationChannel.id == channel_id
        )
    )
    if channel is None:
        raise HTTPException(404, "Channel not found")
    return channel


@router.get("/channels")
async def list_channels(
    session: SessionDep, tenant_id: TenantId, _: Reader
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.tenant_id == tenant_id)
        .order_by(NotificationChannel.name, NotificationChannel.id)
    )
    return [_out(c) for c in rows]


@router.post("/channels", status_code=201)
async def create_channel(
    body: ChannelCreate,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantId,
    principal: Admin,
) -> dict[str, Any]:
    """For webhook channels the response includes ``signing_secret``, shown only this once."""
    target = _clean_target(body.kind, body.target, settings.environment)
    if tenant_id == NO_TENANT:
        tenant_id = await get_or_create_tenant(session, slug=settings.default_tenant_slug)
    new_secret = None
    secret = None
    if body.kind == "webhook":
        secret = body.signing_secret
        if secret is None:
            secret = new_secret = secrets.token_urlsafe(32)
    channel = NotificationChannel(
        tenant_id=tenant_id,
        name=body.name.strip(),
        kind=body.kind,
        target=target,
        signing_secret=secret,
        alert_kinds=_clean_kinds(body.alert_kinds),
        min_severity=body.min_severity,
        digest=body.digest,
        active=body.active,
    )
    session.add(channel)
    await session.flush()
    audit.record(
        session,
        tenant_id,
        principal,
        "notification_channel.create",
        target_type="notification_channel",
        target_id=str(channel.id),
        details=_audit_details(channel),
    )
    await session.commit()
    out = _out(channel)
    if new_secret is not None:
        out["signing_secret"] = new_secret
    return out


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantId,
    principal: Admin,
) -> dict[str, Any]:
    """Partial update. Switching to kind "webhook" without a secret generates one (returned)."""
    channel = await _get(session, tenant_id, channel_id)
    kind = body.kind or channel.kind
    target_changed = body.target is not None or kind != channel.kind
    if target_changed:
        channel.target = _clean_target(kind, body.target or channel.target, settings.environment)
    channel.kind = kind
    if body.name is not None:
        channel.name = body.name.strip()
    if body.alert_kinds is not None:
        channel.alert_kinds = _clean_kinds(body.alert_kinds)
    for field in ("min_severity", "digest", "active"):
        value = getattr(body, field)
        if value is not None:
            setattr(channel, field, value)
    new_secret = None
    if kind == "webhook":
        if body.signing_secret is not None:
            channel.signing_secret = body.signing_secret
        elif not channel.signing_secret:
            channel.signing_secret = new_secret = secrets.token_urlsafe(32)
    else:
        channel.signing_secret = None
    details = _audit_details(channel)
    details["target_changed"] = target_changed
    details["secret_changed"] = body.signing_secret is not None or new_secret is not None
    audit.record(
        session,
        tenant_id,
        principal,
        "notification_channel.update",
        target_type="notification_channel",
        target_id=str(channel.id),
        details=details,
    )
    await session.commit()
    out = _out(channel)
    if new_secret is not None:
        out["signing_secret"] = new_secret
    return out


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: uuid.UUID, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> None:
    """Deletes the channel and its delivery history (the history references it)."""
    channel = await _get(session, tenant_id, channel_id)
    details = _audit_details(channel)
    await session.execute(
        delete(NotificationDelivery).where(
            NotificationDelivery.tenant_id == tenant_id,
            NotificationDelivery.channel_id == channel.id,
        )
    )
    await session.delete(channel)
    audit.record(
        session,
        tenant_id,
        principal,
        "notification_channel.delete",
        target_type="notification_channel",
        target_id=str(channel_id),
        details=details,
    )
    await session.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: TenantId,
    principal: Admin,
) -> dict[str, str]:
    """Send a synthetic alert now. 502 with the (secret-free) reason when delivery fails."""
    channel = await _get(session, tenant_id, channel_id)
    error = None
    try:
        await send_test(channel, settings)
    except NotifyError as exc:
        error = str(exc)
    audit.record(
        session,
        tenant_id,
        principal,
        "notification_channel.test",
        target_type="notification_channel",
        target_id=str(channel.id),
        details={"kind": channel.kind, "status": "failed" if error else "sent"},
    )
    await session.commit()
    if error is not None:
        raise HTTPException(502, error)
    return {"status": "sent"}


@router.get("/deliveries")
async def list_deliveries(
    session: SessionDep,
    tenant_id: TenantId,
    _: Reader,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(NotificationDelivery, NotificationChannel.name)
        .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
        .where(
            NotificationDelivery.tenant_id == tenant_id,
            NotificationChannel.tenant_id == tenant_id,
        )
        .order_by(NotificationDelivery.attempted_at.desc(), NotificationDelivery.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(d.id),
            "channel_id": str(d.channel_id),
            "channel_name": name,
            "alert_event_id": str(d.alert_event_id) if d.alert_event_id is not None else None,
            "status": d.status,
            "error": d.error,
            "attempted_at": (
                d.attempted_at if d.attempted_at.tzinfo else d.attempted_at.replace(tzinfo=UTC)
            ).isoformat(),
        }
        for d, name in rows
    ]
