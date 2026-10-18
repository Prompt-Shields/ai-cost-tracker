"""Allocation rules, split rules, cost-centre view and untagged report (issues #33, #34).

Splits are addressed by ``/allocation/splits/{resource_id_b64}/{deployment}``, where
``resource_id_b64`` is the ARM resource id in URL-safe base64 without padding (ARM ids contain
slashes). ``POST /allocation/splits`` with ``resource_id`` and ``deployment`` in the body is an
equivalent upsert for clients that would rather not encode.
"""

import base64
import binascii
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from ai_cost_tracker import allocation as alloc
from ai_cost_tracker import audit
from ai_cost_tracker.api import queries as q
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import NO_TENANT, CostFilters, Range, SessionDep, TenantId, money
from ai_cost_tracker.db.models import AllocationRule, SplitRule

router = APIRouter(prefix="/allocation", tags=["allocation"], dependencies=[Depends(get_principal)])

Admin = Annotated[Principal, Depends(require_role("admin"))]
PERCENT_TOLERANCE = Decimal("0.0001")
_Label = Annotated[str | None, Field(default=None, max_length=200)]


def _blank_to_none(cls: type, value: str | None) -> str | None:
    """Blank labels mean "not set": store null so they read as unallocated."""
    if isinstance(value, str):
        return value.strip() or None
    return value


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=100, ge=0, le=1_000_000)
    match_type: Literal["tag", "name_pattern", "account"]
    match_key: str | None = Field(default=None, max_length=200)
    match_value: str = Field(min_length=1, max_length=512)
    cost_centre: _Label = None
    team: _Label = None
    app: _Label = None
    active: bool = True

    _strip = field_validator("match_key", "cost_centre", "team", "app")(_blank_to_none)

    @model_validator(mode="after")
    def _check(self) -> "RuleBody":
        self.name = self.name.strip()
        self.match_value = self.match_value.strip()
        if not self.name or not self.match_value:
            raise ValueError("name and match_value must not be blank")
        if self.match_type == "tag" and not self.match_key:
            raise ValueError("match_key (the tag key) is required for tag rules")
        if self.match_type != "tag":
            self.match_key = None
        if self.match_type == "name_pattern":
            _check_glob(self.match_value)
        if not (self.cost_centre or self.team or self.app):
            raise ValueError("set at least one of cost_centre, team, app")
        return self


def _check_glob(pattern: str) -> None:
    """Reject globs fnmatch would silently read literally (an unclosed ``[``)."""
    depth_open = False
    for ch in pattern:
        if ch == "[":
            if depth_open:
                raise ValueError("nested '[' in name pattern")
            depth_open = True
        elif ch == "]":
            depth_open = False
    if depth_open:
        raise ValueError("unclosed '[' in name pattern")


class SplitPart(BaseModel):
    cost_centre: _Label = None
    team: _Label = None
    app: _Label = None
    percent: Decimal = Field(gt=0, le=100)

    _strip = field_validator("cost_centre", "team", "app")(_blank_to_none)


class SplitBody(BaseModel):
    allocations: list[SplitPart] = Field(min_length=1, max_length=100)
    active: bool = True

    @model_validator(mode="after")
    def _check(self) -> "SplitBody":
        total = sum((a.percent for a in self.allocations), Decimal(0))
        if abs(total - 100) > PERCENT_TOLERANCE:
            raise ValueError(f"percents must sum to 100 (got {total})")
        return self


class SplitCreateBody(SplitBody):
    resource_id: str = Field(min_length=1, max_length=512)
    deployment: str = Field(min_length=1, max_length=200)


def _rule_out(r: AllocationRule) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "priority": r.priority,
        "match_type": r.match_type,
        "match_key": r.match_key,
        "match_value": r.match_value,
        "cost_centre": r.cost_centre,
        "team": r.team,
        "app": r.app,
        "active": r.active,
    }


def encode_resource_id(resource_id: str) -> str:
    return base64.urlsafe_b64encode(resource_id.encode()).decode().rstrip("=")


def _decode_resource_id(value: str) -> str:
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        ).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(422, "resource_id_b64 is not valid URL-safe base64") from exc
    if not decoded:
        raise HTTPException(422, "resource_id_b64 is empty")
    return decoded


def _split_out(s: SplitRule) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "resource_id": s.resource_id,
        "resource_id_b64": encode_resource_id(s.resource_id),
        "deployment": s.deployment,
        "label": q.deployment_label(s.resource_id, s.deployment),
        "allocations": s.allocations,
        "active": s.active,
        "approximate": True,
    }


def _require_tenant(tenant_id: uuid.UUID) -> None:
    if tenant_id == NO_TENANT:
        raise HTTPException(409, "No tenant has been set up yet")


# --- rules --------------------------------------------------------------------------------


@router.get("/rules")
async def list_rules(session: SessionDep, tenant_id: TenantId) -> list[dict[str, Any]]:
    """All rules (inactive too) in evaluation order: priority, then name."""
    rows = await session.scalars(
        select(AllocationRule)
        .where(AllocationRule.tenant_id == tenant_id)
        .order_by(AllocationRule.priority, AllocationRule.name)
    )
    return [_rule_out(r) for r in rows]


async def _get_rule(session: SessionDep, tenant_id: uuid.UUID, rule_id: uuid.UUID):
    row = await session.scalar(
        select(AllocationRule).where(
            AllocationRule.tenant_id == tenant_id, AllocationRule.id == rule_id
        )
    )
    if row is None:
        raise HTTPException(404, "Rule not found")
    return row


@router.post("/rules", status_code=201)
async def create_rule(
    body: RuleBody, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> dict[str, Any]:
    _require_tenant(tenant_id)
    row = AllocationRule(tenant_id=tenant_id, **body.model_dump())
    session.add(row)
    await session.flush()
    audit.record(
        session,
        tenant_id,
        principal,
        "allocation.rule_create",
        target_type="allocation_rule",
        target_id=str(row.id),
        details=body.model_dump(),
    )
    await session.commit()
    return _rule_out(row)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID, body: RuleBody, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> dict[str, Any]:
    row = await _get_rule(session, tenant_id, rule_id)
    before = _rule_out(row)
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    audit.record(
        session,
        tenant_id,
        principal,
        "allocation.rule_update",
        target_type="allocation_rule",
        target_id=str(row.id),
        details={"before": before, "after": body.model_dump()},
    )
    await session.commit()
    return _rule_out(row)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> Response:
    row = await _get_rule(session, tenant_id, rule_id)
    audit.record(
        session,
        tenant_id,
        principal,
        "allocation.rule_delete",
        target_type="allocation_rule",
        target_id=str(row.id),
        details=_rule_out(row),
    )
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


# --- splits -------------------------------------------------------------------------------


@router.get("/splits")
async def list_splits(session: SessionDep, tenant_id: TenantId) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(SplitRule)
        .where(SplitRule.tenant_id == tenant_id)
        .order_by(SplitRule.resource_id, SplitRule.deployment)
    )
    return [_split_out(s) for s in rows]


async def _find_split(
    session: SessionDep, tenant_id: uuid.UUID, resource_id: str, deployment: str
) -> SplitRule | None:
    return await session.scalar(
        select(SplitRule).where(
            SplitRule.tenant_id == tenant_id,
            SplitRule.resource_id == resource_id,
            SplitRule.deployment == deployment,
        )
    )


@router.get("/splits/{resource_id_b64}/{deployment}")
async def get_split(
    resource_id_b64: str, deployment: str, session: SessionDep, tenant_id: TenantId
) -> dict[str, Any]:
    row = await _find_split(session, tenant_id, _decode_resource_id(resource_id_b64), deployment)
    if row is None:
        raise HTTPException(404, "Split not found")
    return _split_out(row)


async def _upsert_split(
    session: SessionDep,
    tenant_id: uuid.UUID,
    principal: Principal,
    resource_id: str,
    deployment: str,
    body: SplitBody,
) -> dict[str, Any]:
    _require_tenant(tenant_id)
    allocations = [
        {
            "cost_centre": a.cost_centre,
            "team": a.team,
            "app": a.app,
            "percent": format(a.percent.normalize(), "f"),
        }
        for a in body.allocations
    ]
    row = await _find_split(session, tenant_id, resource_id, deployment)
    created = row is None
    if row is None:
        row = SplitRule(tenant_id=tenant_id, resource_id=resource_id, deployment=deployment)
        session.add(row)
    row.allocations = allocations
    row.active = body.active
    await session.flush()
    audit.record(
        session,
        tenant_id,
        principal,
        "allocation.split_create" if created else "allocation.split_update",
        target_type="split_rule",
        target_id=q.deployment_label(resource_id, deployment),
        details={"resource_id": resource_id, "allocations": allocations, "active": body.active},
    )
    await session.commit()
    return _split_out(row)


@router.put("/splits/{resource_id_b64}/{deployment}")
async def put_split(
    resource_id_b64: str,
    deployment: str,
    body: SplitBody,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Admin,
) -> dict[str, Any]:
    resource_id = _decode_resource_id(resource_id_b64)
    return await _upsert_split(session, tenant_id, principal, resource_id, deployment, body)


@router.post("/splits")
async def post_split(
    body: SplitCreateBody, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> dict[str, Any]:
    return await _upsert_split(
        session, tenant_id, principal, body.resource_id, body.deployment, body
    )


@router.delete("/splits/{resource_id_b64}/{deployment}", status_code=204)
async def delete_split(
    resource_id_b64: str,
    deployment: str,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Admin,
) -> Response:
    resource_id = _decode_resource_id(resource_id_b64)
    row = await _find_split(session, tenant_id, resource_id, deployment)
    if row is None:
        raise HTTPException(404, "Split not found")
    audit.record(
        session,
        tenant_id,
        principal,
        "allocation.split_delete",
        target_type="split_rule",
        target_id=q.deployment_label(resource_id, deployment),
        details={"resource_id": resource_id, "allocations": row.allocations},
    )
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


# --- reports ------------------------------------------------------------------------------


@router.get("/cost-centres/daily")
async def cost_centres_daily(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    level: Annotated[Literal["cost_centre", "team", "app"], Query()] = "cost_centre",
) -> dict[str, Any]:
    """Cost per day and cost centre / team / app. Every dollar of ``/costs/daily`` lands in a
    key or in "unallocated"; ``approximate`` marks rows touched by a split or by splitting a
    resource's bill across deployments."""
    rows = await alloc.cost_centres_daily(
        session, tenant_id, rng.start, rng.end, filters, level=level
    )
    return {
        "currency": await q.currency(session, tenant_id, rng.start, rng.end),
        "rows": [
            {
                "day": r.day.isoformat(),
                "key": r.key,
                "cost": money(r.cost),
                "approximate": r.approximate,
            }
            for r in rows
        ],
    }


@router.get("/untagged")
async def untagged(rng: Range, session: SessionDep, tenant_id: TenantId) -> dict[str, Any]:
    """Deployments missing any of ``REQUIRED_TAGS``, with their cost over the range."""
    report = await alloc.untagged_report(session, tenant_id, rng.start, rng.end)
    return {
        "total": money(report.total),
        "unattributed_cost": money(report.unattributed_cost),
        "currency": await q.currency(session, tenant_id, rng.start, rng.end),
        "required_tags": alloc.REQUIRED_TAGS,
        "deployments": [
            {
                "id": d.id,
                "name": d.name,
                "account_id": d.account_id,
                "resource_id": d.resource_id,
                "account_name": d.account_name,
                "model": d.model,
                "cost": money(d.cost),
                "missing_tags": d.missing_tags,
            }
            for d in report.deployments
        ],
    }
