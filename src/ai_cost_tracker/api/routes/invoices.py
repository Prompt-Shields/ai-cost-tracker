"""Invoice-period view per subscription (ARCHITECTURE.md §4.5, issue #36).

Totals are pre-credit cost: Azure credits reduce the invoice, not the usage cost.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, TenantId, Today, money
from ai_cost_tracker.db.models import CloudAccountRow
from ai_cost_tracker.invoices import MAX_PERIOD_DAY, invoice_periods

router = APIRouter(tags=["invoices"], dependencies=[Depends(get_principal)])

MAX_PERIODS = 24


class BillingPeriodDayBody(BaseModel):
    # Required key; null switches the subscription back to calendar months.
    billing_period_day: int | None = Field(..., ge=1, le=MAX_PERIOD_DAY)


@router.get("/invoices/periods")
async def list_periods(
    session: SessionDep,
    tenant_id: TenantId,
    today: Today,
    account_id: Annotated[str, Query(description="Subscription / account id")],
    count: Annotated[int, Query(ge=1, le=MAX_PERIODS)] = 6,
    cloud: str = "azure",
) -> dict[str, Any]:
    """The open period and the closed ones before it, newest first."""
    account = await _account(session, tenant_id, cloud, account_id)
    periods = await invoice_periods(
        session, tenant_id, account_id, account.billing_period_day, today, count
    )
    return {
        "account_id": account.account_id,
        "billing_period_day": account.billing_period_day,
        "periods": [
            {
                "start": p.start.isoformat(),
                "end": p.end.isoformat(),
                "billed_cost": money(p.billed_cost),
                "estimated_cost": money(p.estimated_cost),
                "currency": p.currency,
                "status": p.status,
            }
            for p in periods
        ],
    }


@router.put("/accounts/{account_id}/billing-period-day")
async def set_billing_period_day(
    account_id: str,
    body: BillingPeriodDayBody,
    session: SessionDep,
    tenant_id: TenantId,
    admin: Annotated[Principal, Depends(require_role("admin"))],
    cloud: str = "azure",
) -> dict[str, Any]:
    account = await _account(session, tenant_id, cloud, account_id)
    previous = account.billing_period_day
    account.billing_period_day = body.billing_period_day
    audit.record(
        session,
        tenant_id,
        admin,
        "account.billing_period_day",
        target_type="cloud_account",
        target_id=account.account_id,
        details={"from": previous, "to": body.billing_period_day, "cloud": cloud},
    )
    await session.commit()
    return {"account_id": account.account_id, "billing_period_day": account.billing_period_day}


async def _account(
    session: AsyncSession, tenant_id: uuid.UUID, cloud: str, account_id: str
) -> CloudAccountRow:
    account = await session.scalar(
        select(CloudAccountRow).where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.cloud == cloud,
            CloudAccountRow.account_id == account_id,
        )
    )
    if account is None:
        raise HTTPException(404, f"Unknown account {account_id}")
    return account
