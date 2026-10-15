"""Shared request dependencies for the read API: tenant lookup and date-range parsing."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.api.queries import Filters
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import Tenant
from ai_cost_tracker.db.session import get_session

# Matches no rows. Returned when the tenant hasn't been created yet, so reads come back empty
# instead of 404ing, and a GET never creates anything.
NO_TENANT = uuid.UUID(int=0)

MAX_RANGE_DAYS = 400
DEFAULT_RANGE_DAYS = 30

_MONEY_PLACES = Decimal("0.0000000001")  # matches Numeric(20, 10) in db/models.py


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_tenant_id(session: SessionDep, settings: SettingsDep) -> uuid.UUID:
    """The v1 single tenant (``settings.default_tenant_slug``), or ``NO_TENANT``."""
    tenant_id = await session.scalar(
        select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
    )
    return tenant_id or NO_TENANT


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date  # inclusive


def date_range(
    start: Annotated[date | None, Query(description="First day, inclusive")] = None,
    end: Annotated[date | None, Query(description="Last day, inclusive (default: today)")] = None,
) -> DateRange:
    """Inclusive day range; 422 when reversed or longer than MAX_RANGE_DAYS."""
    if end is None:
        end = date.today() if start is None else start + timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start is None:
        start = end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if start > end:
        raise HTTPException(422, "start must be on or before end")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(422, f"range may span at most {MAX_RANGE_DAYS} days")
    return DateRange(start, end)


def get_today() -> date:
    """Today's UTC date; a dependency so tests can pin "today" and "month to date"."""
    return datetime.now(UTC).date()


def cost_filters(
    account_id: Annotated[str | None, Query(description="Subscription / account id")] = None,
    model: Annotated[str | None, Query(description='Model, or "unmapped"')] = None,
    tag: Annotated[
        str | None,
        Query(
            description="key:value tag on the deployment. Billed cost is per resource, so a "
            "resource whose deployments carry different tags is split approximately "
            "(ARCHITECTURE.md §4.4)",
            pattern=r"^[^:]+:.*$",
        ),
    ] = None,
) -> Filters:
    parsed = None
    if tag is not None:
        key, value = tag.split(":", 1)
        parsed = (key, value)
    return Filters(account_id=account_id, model=model, tag=parsed)


TenantId = Annotated[uuid.UUID, Depends(get_tenant_id)]
Range = Annotated[DateRange, Depends(date_range)]
Today = Annotated[date, Depends(get_today)]
CostFilters = Annotated[Filters, Depends(cost_filters)]


def money(value: Decimal | float | int | None) -> str:
    """Serialise money as a fixed-scale string so both dialects render identically."""
    # SQLite may hand back floats for SUM(); go through str() to avoid binary noise.
    return format(Decimal(str(value or 0)).quantize(_MONEY_PLACES), "f")
