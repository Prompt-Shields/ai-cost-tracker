"""API key management (issue #35). The full key is returned once, at creation; never again."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from ai_cost_tracker import api_keys, audit
from ai_cost_tracker.api.auth import Principal, is_api_key, require_role
from ai_cost_tracker.api.deps import NO_TENANT, SessionDep, TenantId
from ai_cost_tracker.db.models import ApiKey

router = APIRouter(tags=["api-keys"])

Admin = Annotated[Principal, Depends(require_role("admin"))]


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=lambda: list(api_keys.DEFAULT_ROLES))

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("roles must not be empty")
        bad = set(v) - api_keys.KEY_ROLES
        if bad:
            raise ValueError(f"roles must be a subset of {sorted(api_keys.KEY_ROLES)}")
        return sorted(set(v))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat()


def _json(row: ApiKey) -> dict[str, Any]:
    """Public view of a key: never the hash, never the key."""
    return {
        "id": str(row.id),
        "name": row.name,
        "prefix": row.prefix,
        "roles": list(row.roles or []),
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "last_used_at": _iso(row.last_used_at),
        "revoked_at": _iso(row.revoked_at),
    }


def _no_key_management(principal: Principal) -> None:
    # A leaked key must not be able to mint or keep alive further keys.
    if is_api_key(principal):
        raise HTTPException(403, "API keys cannot manage API keys; sign in instead")


@router.get("/api-keys")
async def list_api_keys(
    session: SessionDep, tenant_id: TenantId, principal: Admin
) -> list[dict[str, Any]]:
    return [_json(r) for r in await api_keys.list_keys(session, tenant_id)]


@router.post("/api-keys", status_code=201)
async def create_api_key(
    body: KeyCreate, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> dict[str, Any]:
    _no_key_management(principal)
    if tenant_id == NO_TENANT:
        raise HTTPException(409, "tenant not set up yet")
    if "admin" in body.roles and "owner" not in principal.roles:
        raise HTTPException(403, "Only an owner can create admin keys")
    new = api_keys.generate()
    row = ApiKey(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=body.name,
        prefix=new.prefix,
        key_hash=new.key_hash,
        roles=body.roles,
        created_by=principal.email or principal.name or principal.oid,
        created_at=datetime.now(UTC),
    )
    session.add(row)
    audit.record(
        session,
        tenant_id,
        principal,
        "api_key.create",
        target_type="api_key",
        target_id=str(row.id),
        details={"name": row.name, "prefix": row.prefix, "roles": row.roles},
    )
    await session.commit()
    return _json(row) | {"key": new.key}


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: uuid.UUID, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> dict[str, Any]:
    _no_key_management(principal)
    row = await api_keys.get_key(session, tenant_id, key_id)
    if row is None:
        raise HTTPException(404, "API key not found")
    if row.revoked_at is None:  # revoking twice keeps the first time
        row.revoked_at = datetime.now(UTC)
        audit.record(
            session,
            tenant_id,
            principal,
            "api_key.revoke",
            target_type="api_key",
            target_id=str(row.id),
            details={"name": row.name, "prefix": row.prefix},
        )
        await session.commit()
    return _json(row)
