"""Monthly budgets with month-to-date spend, forecast and status (issue #29).

Reads need any signed-in principal; create/update/delete need ``admin`` and write an audit
row in the same transaction. Spend and forecast come from ``ai_cost_tracker.budgets``.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit, budgets
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import NO_TENANT, SessionDep, SettingsDep, TenantId, Today, money
from ai_cost_tracker.api.queries import load_inventory
from ai_cost_tracker.db.models import Budget, DeploymentRow
from ai_cost_tracker.ingest import get_or_create_tenant

router = APIRouter(tags=["budgets"], dependencies=[Depends(get_principal)])

Admin = Annotated[Principal, Depends(require_role("admin"))]


class BudgetBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scope_type: Literal["all", "account", "model", "deployment", "tag"]
    scope_value: str | None = Field(default=None, max_length=512)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=10)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    thresholds: list[float] = Field(default_factory=lambda: [0.8, 1.0], min_length=1)
    active: bool = True

    @field_validator("thresholds")
    @classmethod
    def _thresholds(cls, value: list[float]) -> list[float]:
        if any(not 0 < t <= 2 for t in value):
            raise ValueError("each threshold must be in (0, 2]")
        return sorted(set(value))

    @model_validator(mode="after")
    def _scope(self) -> "BudgetBody":
        value = (self.scope_value or "").strip()
        if self.scope_type == "all":
            self.scope_value = None
            return self
        if not value:
            raise ValueError(f"scope_value is required for scope_type {self.scope_type!r}")
        if self.scope_type == "tag":
            key, sep, _ = value.partition(":")
            if not sep or not key.strip():
                raise ValueError('tag scope_value must look like "key:value"')
        self.scope_value = value
        return self


def _out(budget: Budget, ev: budgets.Evaluation) -> dict[str, Any]:
    return {
        "id": str(budget.id),
        "name": budget.name,
        "scope_type": budget.scope_type,
        "scope_value": budget.scope_value,
        "amount": money(budget.amount),
        "currency": budget.currency,
        "thresholds": [float(t) for t in budget.thresholds or []],
        "active": budget.active,
        "month_to_date": money(ev.month_to_date),
        "forecast_month_end": money(ev.forecast_month_end),
        "percent_used": ev.percent_used,
        "status": ev.status,
    }


def _audit_details(budget: Budget) -> dict[str, Any]:
    return {
        "name": budget.name,
        "scope_type": budget.scope_type,
        "scope_value": budget.scope_value,
        "amount": money(budget.amount),
        "currency": budget.currency,
        "thresholds": [float(t) for t in budget.thresholds or []],
        "active": budget.active,
    }


async def _check_deployment(session: AsyncSession, tenant_id: uuid.UUID, body: BudgetBody) -> None:
    if body.scope_type != "deployment":
        return
    try:
        dep_id = uuid.UUID(body.scope_value or "")
    except ValueError as exc:
        raise HTTPException(422, "scope_value must be a deployment id") from exc
    found = await session.scalar(
        select(DeploymentRow.id).where(
            DeploymentRow.tenant_id == tenant_id, DeploymentRow.id == dep_id
        )
    )
    if found is None:
        raise HTTPException(422, "deployment not found")
    body.scope_value = str(dep_id)


async def _get(session: AsyncSession, tenant_id: uuid.UUID, budget_id: str) -> Budget:
    try:
        key = uuid.UUID(budget_id)
    except ValueError as exc:
        raise HTTPException(404, "budget not found") from exc
    budget = await session.scalar(
        select(Budget).where(Budget.tenant_id == tenant_id, Budget.id == key)
    )
    if budget is None:
        raise HTTPException(404, "budget not found")
    return budget


def _apply(budget: Budget, body: BudgetBody) -> None:
    budget.name = body.name.strip()
    budget.scope_type = body.scope_type
    budget.scope_value = body.scope_value
    budget.amount = body.amount
    budget.currency = body.currency
    budget.thresholds = body.thresholds
    budget.active = body.active


async def _evaluated(
    session: AsyncSession, tenant_id: uuid.UUID, budget: Budget, today: date
) -> dict[str, Any]:
    return _out(budget, await budgets.evaluate(session, tenant_id, budget, today))


@router.get("/budgets")
async def list_budgets(
    session: SessionDep, tenant_id: TenantId, today: Today
) -> list[dict[str, Any]]:
    rows = (
        await session.scalars(
            select(Budget).where(Budget.tenant_id == tenant_id).order_by(Budget.name, Budget.id)
        )
    ).all()
    inventory = await load_inventory(session, tenant_id)
    return [_out(b, await budgets.evaluate(session, tenant_id, b, today, inventory)) for b in rows]


@router.post("/budgets", status_code=201)
async def create_budget(
    body: BudgetBody,
    session: SessionDep,
    tenant_id: TenantId,
    settings: SettingsDep,
    principal: Admin,
    today: Today,
) -> dict[str, Any]:
    if tenant_id == NO_TENANT:
        tenant_id = await get_or_create_tenant(session, slug=settings.default_tenant_slug)
    await _check_deployment(session, tenant_id, body)
    budget = Budget(tenant_id=tenant_id, id=uuid.uuid4())
    _apply(budget, body)
    session.add(budget)
    audit.record(
        session,
        tenant_id,
        principal,
        "budget.create",
        target_type="budget",
        target_id=str(budget.id),
        details=_audit_details(budget),
    )
    await session.commit()
    return await _evaluated(session, tenant_id, budget, today)


@router.put("/budgets/{budget_id}")
async def update_budget(
    budget_id: str,
    body: BudgetBody,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Admin,
    today: Today,
) -> dict[str, Any]:
    budget = await _get(session, tenant_id, budget_id)
    await _check_deployment(session, tenant_id, body)
    before = _audit_details(budget)
    _apply(budget, body)
    after = _audit_details(budget)
    changed = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
    audit.record(
        session,
        tenant_id,
        principal,
        "budget.update",
        target_type="budget",
        target_id=str(budget.id),
        details={"name": budget.name, "changes": changed},
    )
    await session.commit()
    return await _evaluated(session, tenant_id, budget, today)


@router.delete("/budgets/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: str, session: SessionDep, tenant_id: TenantId, principal: Admin
) -> Response:
    budget = await _get(session, tenant_id, budget_id)
    audit.record(
        session,
        tenant_id,
        principal,
        "budget.delete",
        target_type="budget",
        target_id=str(budget.id),
        details=_audit_details(budget),
    )
    await session.delete(budget)
    await session.commit()
    return Response(status_code=204)
