"""Cost and token aggregates over a day range. All grouping happens in SQL."""

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ai_cost_tracker.api.auth import get_principal
from ai_cost_tracker.api.deps import DateRange, Range, SessionDep, TenantId, money
from ai_cost_tracker.db.models import CostLine, TokenUsageRow

router = APIRouter(tags=["costs"], dependencies=[Depends(get_principal)])

UNMAPPED = "unmapped"
DEFAULT_CURRENCY = "USD"
_PER_1M_PLACES = Decimal("0.0001")


def _usage_day(session: AsyncSession) -> ColumnElement:
    """UTC calendar day of ``token_usage.bucket_start``.

    ``CAST(x AS DATE)`` is wrong on SQLite (DATE has NUMERIC affinity, so it yields the year)
    and on Postgres it follows the session time zone, so pin UTC there explicitly.
    """
    col = TokenUsageRow.bucket_start
    if session.get_bind().dialect.name == "postgresql":
        return cast(func.timezone("UTC", col), Date)
    return func.date(col)


def _usage_window(rng: DateRange) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, end+1 00:00 UTC): a range on the raw column keeps the index usable."""
    lo = datetime.combine(rng.start, time.min, tzinfo=UTC)
    return lo, datetime.combine(rng.end + timedelta(days=1), time.min, tzinfo=UTC)


def _day(value: Any) -> str:
    # Postgres returns a date, SQLite func.date() a 'YYYY-MM-DD' string.
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _currency(session: AsyncSession, tenant_id: uuid.UUID, rng: DateRange) -> str:
    """The billing currency in range. v1 subscriptions all bill in one currency."""
    currencies = (
        await session.scalars(
            select(CostLine.billing_currency)
            .where(
                CostLine.tenant_id == tenant_id,
                CostLine.charge_period_start.between(rng.start, rng.end),
            )
            .distinct()
        )
    ).all()
    return currencies[0] if len(currencies) == 1 else DEFAULT_CURRENCY


@router.get("/costs/daily")
async def costs_daily(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
) -> dict[str, Any]:
    model = func.coalesce(CostLine.ai_model, UNMAPPED)
    result = await session.execute(
        select(CostLine.charge_period_start, model, func.sum(CostLine.billed_cost))
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.charge_period_start.between(rng.start, rng.end),
        )
        .group_by(CostLine.charge_period_start, model)
        .order_by(CostLine.charge_period_start, model)
    )
    return {
        "currency": await _currency(session, tenant_id, rng),
        "rows": [
            {"day": _day(day), "model": m, "billed_cost": money(cost)} for day, m, cost in result
        ],
    }


@router.get("/usage/daily")
async def usage_daily(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
) -> dict[str, Any]:
    day = _usage_day(session)
    lo, hi = _usage_window(rng)
    result = await session.execute(
        select(
            day,
            TokenUsageRow.deployment,
            TokenUsageRow.model,
            func.sum(TokenUsageRow.input_tokens),
            func.sum(TokenUsageRow.output_tokens),
            func.sum(TokenUsageRow.requests),
        )
        .where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.bucket_start >= lo,
            TokenUsageRow.bucket_start < hi,
        )
        .group_by(day, TokenUsageRow.deployment, TokenUsageRow.model)
        .order_by(day, TokenUsageRow.deployment, TokenUsageRow.model)
    )
    return {
        "rows": [
            {
                "day": _day(d),
                "deployment": dep,
                "model": m,
                "input_tokens": int(inp or 0),
                "output_tokens": int(out or 0),
                "requests": int(req or 0),
            }
            for d, dep, m, inp, out, req in result
        ]
    }


@router.get("/summary")
async def summary(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
) -> dict[str, Any]:
    cost_model = func.coalesce(CostLine.ai_model, UNMAPPED).label("model")
    cost = (
        select(cost_model, func.sum(CostLine.billed_cost).label("billed_cost"))
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.charge_period_start.between(rng.start, rng.end),
        )
        .group_by(cost_model)
        .subquery()
    )
    lo, hi = _usage_window(rng)
    usage_model = func.coalesce(TokenUsageRow.model, UNMAPPED).label("model")
    usage = (
        select(
            usage_model,
            func.sum(TokenUsageRow.input_tokens).label("input_tokens"),
            func.sum(TokenUsageRow.output_tokens).label("output_tokens"),
        )
        .where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.bucket_start >= lo,
            TokenUsageRow.bucket_start < hi,
        )
        .group_by(usage_model)
        .subquery()
    )
    # Cost-driven: a model with tokens but no billed cost yet (billing lags ~1 day) is
    # left out rather than shown as free.
    result = await session.execute(
        select(cost.c.model, cost.c.billed_cost, usage.c.input_tokens, usage.c.output_tokens)
        .select_from(cost.outerjoin(usage, usage.c.model == cost.c.model))
        .order_by(cost.c.billed_cost.desc(), cost.c.model)
    )

    by_model = []
    total = Decimal(0)
    for model, billed, inp, out in result:
        billed_dec = Decimal(money(billed))
        total += billed_dec
        inp, out = int(inp or 0), int(out or 0)
        tokens = inp + out
        per_1m = (
            format((billed_dec / tokens * 1_000_000).quantize(_PER_1M_PLACES), "f")
            if tokens
            else None
        )
        by_model.append(
            {
                "model": model,
                "billed_cost": money(billed_dec),
                "input_tokens": inp,
                "output_tokens": out,
                "cost_per_1m_tokens": per_1m,
            }
        )
    return {
        "currency": await _currency(session, tenant_id, rng),
        "total_cost": money(total),
        "by_model": by_model,
    }
