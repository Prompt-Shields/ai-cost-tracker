"""Cost, estimate and token aggregation shared by the cost and inventory routes.

Everything is loaded into *cells*: one bucket per (day, account, resource, deployment, model).
SQL does the heavy grouping (hourly tokens and per-meter cost lines collapse to cells); the
cheap parts that need cross-dialect JSON or per-day rules (tag matching, attributing a
resource's billed cost to its deployments, billed-vs-estimated choice) run in Python.

Billed vs estimated (ARCHITECTURE.md §13 M1, issue #21): an (account, day) is "billed" once
any cost line has arrived for it (or Reconciliation says billed/finalised). For billed
account-days the billed cost is the cost; otherwise the token x price estimate stands in.

Deployment attribution (§4.4): Azure bills per Cognitive Services account (resource) and
meter, not per deployment. A cost line goes to the resource's deployment serving its model;
when several deployments on one resource serve the same model it is split by that day's
tokens (even split when there are none). That split, and tag filtering/grouping that depends
on it, is approximate: billing data alone can't tell those deployments apart. Cost lines
whose model no deployment serves stay on the resource as "unattributed" and take the tags
all of the resource's deployments agree on.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ai_cost_tracker.connectors.base import is_provisioned
from ai_cost_tracker.db.models import (
    CostLine,
    DailyEstimate,
    DeploymentRow,
    Reconciliation,
    TokenUsageRow,
)
from ai_cost_tracker.normalise.azure_meters import PROVISIONED_MODEL

UNMAPPED = "unmapped"
UNTAGGED = "untagged"
UNATTRIBUTED = "unattributed"
DEFAULT_CURRENCY = "USD"
STATUSES = ("estimated", "billed", "finalised")  # ascending: how settled a day's cost is
INPUT_DIRECTIONS = frozenset({"input", "cached_input"})
OUTPUT_DIRECTIONS = frozenset({"output"})
_ZERO = Decimal(0)
_PER_1M_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class Filters:
    """Request filters. ``resource_id``/``deployment`` narrow to one deployment (detail page)."""

    account_id: str | None = None
    model: str | None = None
    tag: tuple[str, str] | None = None
    resource_id: str | None = None
    deployment: str | None = None


@dataclass(frozen=True)
class CellKey:
    day: date
    account_id: str
    resource_id: str
    deployment: str | None  # None: billed cost no deployment could be matched to
    model: str


@dataclass
class Cell:
    billed: Decimal = _ZERO
    billed_input: Decimal = _ZERO
    billed_output: Decimal = _ZERO
    estimated: Decimal = _ZERO
    estimated_input: Decimal = _ZERO
    estimated_output: Decimal = _ZERO
    input_tokens: int = 0
    output_tokens: int = 0
    has_cost: bool = False  # any cost line or estimate row landed here (not tokens alone)


class Inventory:
    """All of a tenant's deployments (deleted ones too: their history still costs money)."""

    def __init__(self, rows: Iterable[DeploymentRow]):
        self.rows = list(rows)
        self.by_key = {(r.resource_id, r.name): r for r in self.rows}
        self.by_resource: dict[str, list[DeploymentRow]] = defaultdict(list)
        for r in self.rows:
            self.by_resource[r.resource_id].append(r)

    def tags(self, resource_id: str, deployment: str | None) -> dict[str, Any]:
        row = self.by_key.get((resource_id, deployment)) if deployment else None
        if row is not None:
            return row.tags or {}
        # Unknown or unattributed: only tags every deployment on the resource agrees on.
        siblings = [r.tags or {} for r in self.by_resource.get(resource_id, [])]
        if not siblings:
            return {}
        return {k: v for k, v in siblings[0].items() if all(s.get(k) == v for s in siblings)}

    def candidates(self, resource_id: str, model: str) -> list[DeploymentRow]:
        rows = self.by_resource.get(resource_id, [])
        if model == PROVISIONED_MODEL:
            # PTU capacity meters name no model: they belong to the provisioned deployments.
            return [r for r in rows if is_provisioned(r.sku)]
        return [r for r in rows if (r.model or "").lower() == model.lower()]


def deployment_label(resource_id: str, deployment: str | None) -> str:
    """``<account resource name>/<deployment>``: readable and unique within a tenant."""
    resource = resource_id.rstrip("/").rsplit("/", 1)[-1] or resource_id
    return f"{resource}/{deployment or UNATTRIBUTED}"


@dataclass
class Dataset:
    cells: dict[CellKey, Cell] = field(default_factory=dict)
    # (day, account_id) -> estimated | billed | finalised
    status: dict[tuple[date, str], str] = field(default_factory=dict)

    def day_status(self, day: date, account_id: str) -> str:
        return self.status.get((day, account_id), "estimated")

    def effective(self, key: CellKey, cell: Cell) -> tuple[Decimal, Decimal, Decimal]:
        """(total, input, output) cost to report: billed once billed, else the estimate."""
        if self.day_status(key.day, key.account_id) == "estimated":
            return cell.estimated, cell.estimated_input, cell.estimated_output
        return cell.billed, cell.billed_input, cell.billed_output


def min_status(statuses: Iterable[str]) -> str:
    """Least settled status: a row is only "billed" when every account in it is."""
    return min(statuses, key=STATUSES.index, default="estimated")


def per_1m(cost: Decimal, tokens: int) -> str | None:
    if not tokens:
        return None
    return format((cost / tokens * 1_000_000).quantize(_PER_1M_PLACES), "f")


def usage_day(session: AsyncSession) -> ColumnElement:
    """UTC calendar day of ``token_usage.bucket_start``.

    ``CAST(x AS DATE)`` is wrong on SQLite (DATE has NUMERIC affinity, so it yields the year)
    and on Postgres it follows the session time zone, so pin UTC there explicitly.
    """
    col = TokenUsageRow.bucket_start
    if session.get_bind().dialect.name == "postgresql":
        return cast(func.timezone("UTC", col), Date)
    return func.date(col)


def usage_window(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00 UTC, end+1 00:00 UTC): a range on the raw column keeps the index usable."""
    lo = datetime.combine(start, time.min, tzinfo=UTC)
    return lo, datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)


def as_date(value: Any) -> date:
    # Postgres returns a date, SQLite func.date() a 'YYYY-MM-DD' string.
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _dec(value: Any) -> Decimal:
    # SQLite may hand back floats for SUM(); go through str() to avoid binary noise.
    return Decimal(str(value or 0))


def _model_filter(col: Any, model: str | None) -> list[ColumnElement]:
    if model is None:
        return []
    return [col.is_(None)] if model == UNMAPPED else [col == model]


async def load_inventory(session: AsyncSession, tenant_id: uuid.UUID) -> Inventory:
    rows = await session.scalars(select(DeploymentRow).where(DeploymentRow.tenant_id == tenant_id))
    return Inventory(rows)


def _resources_for(inventory: Inventory, filters: Filters) -> set[str] | None:
    """Resources the SQL may be narrowed to (IN list), or None for no narrowing.

    The tag test runs here in Python so the JSON column works the same on SQLite and Postgres.
    """
    if filters.resource_id is not None:
        return {filters.resource_id}
    if filters.tag is None:
        return None
    key, value = filters.tag
    return {r.resource_id for r in inventory.rows if (r.tags or {}).get(key) == value}


def _keep(inventory: Inventory, filters: Filters, key: CellKey) -> bool:
    if filters.deployment is not None and key.deployment != filters.deployment:
        return False
    if filters.tag is not None:
        tag_key, value = filters.tag
        if inventory.tags(key.resource_id, key.deployment).get(tag_key) != value:
            return False
    return True


async def load(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    filters: Filters,
    inventory: Inventory,
) -> Dataset:
    """All cells for the tenant in [start, end] that pass ``filters``, plus day statuses."""
    resources = _resources_for(inventory, filters)
    data = Dataset()
    if resources is not None and not resources:
        return data
    cells: dict[CellKey, Cell] = defaultdict(Cell)

    # Tokens: also the weights for splitting a resource's cost across same-model deployments,
    # so they are narrowed by resource only, never by deployment or tag, before attribution.
    day = usage_day(session)
    lo, hi = usage_window(start, end)
    usage_q = (
        select(
            day,
            TokenUsageRow.account_id,
            TokenUsageRow.resource_id,
            TokenUsageRow.deployment,
            TokenUsageRow.model,
            func.sum(TokenUsageRow.input_tokens),
            func.sum(TokenUsageRow.output_tokens),
        )
        .where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.bucket_start >= lo,
            TokenUsageRow.bucket_start < hi,
            *_model_filter(TokenUsageRow.model, filters.model),
        )
        .group_by(
            day,
            TokenUsageRow.account_id,
            TokenUsageRow.resource_id,
            TokenUsageRow.deployment,
            TokenUsageRow.model,
        )
    )
    if filters.account_id is not None:
        usage_q = usage_q.where(TokenUsageRow.account_id == filters.account_id)
    if resources is not None:
        usage_q = usage_q.where(TokenUsageRow.resource_id.in_(resources))
    weights: dict[tuple[date, str, str], int] = defaultdict(int)
    for d, account, resource, dep, model, inp, out in await session.execute(usage_q):
        d = as_date(d)
        cell = cells[CellKey(d, account, resource, dep, model or UNMAPPED)]
        cell.input_tokens += int(inp or 0)
        cell.output_tokens += int(out or 0)
        weights[(d, resource, dep)] += int(inp or 0) + int(out or 0)

    est_q = (
        select(
            DailyEstimate.day,
            DailyEstimate.account_id,
            DailyEstimate.resource_id,
            DailyEstimate.deployment,
            DailyEstimate.model,
            func.sum(DailyEstimate.estimated_cost),
            func.sum(DailyEstimate.input_cost),
            func.sum(DailyEstimate.output_cost),
        )
        .where(
            DailyEstimate.tenant_id == tenant_id,
            DailyEstimate.day.between(start, end),
            *_model_filter(DailyEstimate.model, filters.model),
        )
        .group_by(
            DailyEstimate.day,
            DailyEstimate.account_id,
            DailyEstimate.resource_id,
            DailyEstimate.deployment,
            DailyEstimate.model,
        )
    )
    if filters.account_id is not None:
        est_q = est_q.where(DailyEstimate.account_id == filters.account_id)
    if resources is not None:
        est_q = est_q.where(DailyEstimate.resource_id.in_(resources))
    for d, account, resource, dep, model, total, inp, out in await session.execute(est_q):
        cell = cells[CellKey(as_date(d), account, resource, dep, model or UNMAPPED)]
        cell.estimated += _dec(total)
        cell.estimated_input += _dec(inp)
        cell.estimated_output += _dec(out)
        cell.has_cost = True

    cost_q = (
        select(
            CostLine.charge_period_start,
            CostLine.sub_account_id,
            CostLine.resource_id,
            CostLine.ai_model,
            CostLine.ai_direction,
            func.sum(CostLine.billed_cost),
        )
        .where(
            CostLine.tenant_id == tenant_id,
            CostLine.charge_period_start.between(start, end),
            *_model_filter(CostLine.ai_model, filters.model),
        )
        .group_by(
            CostLine.charge_period_start,
            CostLine.sub_account_id,
            CostLine.resource_id,
            CostLine.ai_model,
            CostLine.ai_direction,
        )
    )
    if filters.account_id is not None:
        cost_q = cost_q.where(CostLine.sub_account_id == filters.account_id)
    if resources is not None:
        cost_q = cost_q.where(CostLine.resource_id.in_(resources))
    for d, account, resource, model, direction, billed in await session.execute(cost_q):
        d, model = as_date(d), model or UNMAPPED
        for dep, share in _attribute(inventory, weights, d, resource, model, _dec(billed)):
            cell = cells[CellKey(d, account, resource, dep, model)]
            cell.billed += share
            if direction in INPUT_DIRECTIONS:
                cell.billed_input += share
            elif direction in OUTPUT_DIRECTIONS:
                cell.billed_output += share
            cell.has_cost = True

    data.cells = {k: c for k, c in cells.items() if _keep(inventory, filters, k)}
    data.status = await _statuses(session, tenant_id, start, end, filters.account_id)
    return data


def _attribute(
    inventory: Inventory,
    weights: dict[tuple[date, str, str], int],
    day: date,
    resource_id: str,
    model: str,
    amount: Decimal,
) -> list[tuple[str | None, Decimal]]:
    """Split one resource's billed amount for a model across the deployments serving it."""
    candidates = inventory.candidates(resource_id, model)
    if not candidates:
        return [(None, amount)]
    if len(candidates) == 1:
        return [(candidates[0].name, amount)]
    w = [weights.get((day, resource_id, c.name), 0) for c in candidates]
    if sum(w):
        # Deployments without tokens that day get nothing (and no zero-cost row).
        candidates, w = [c for c, x in zip(candidates, w, strict=True) if x], [x for x in w if x]
    else:
        # No tokens to go by: split evenly over live deployments (all, if none are live).
        live = [c for c in candidates if c.deleted_at is None] or candidates
        candidates, w = live, [1] * len(live)
    total = sum(w)
    shares, given = [], _ZERO
    for i, (c, weight) in enumerate(zip(candidates, w, strict=True)):
        share = amount - given if i == len(candidates) - 1 else amount * weight / total
        given += share
        shares.append((c.name, share))
    return shares


async def _statuses(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    account_id: str | None,
) -> dict[tuple[date, str], str]:
    """Day status per (day, account), regardless of model/tag filters: billing arrives per
    subscription and day, so a day is billed for every model once any cost line is in.

    Reconciliation can lag the cost ingest, so cost lines alone also make a day billed.
    """
    recon_q = select(Reconciliation.day, Reconciliation.account_id, Reconciliation.day_status)
    recon_q = recon_q.where(
        Reconciliation.tenant_id == tenant_id, Reconciliation.day.between(start, end)
    )
    billed_q = (
        select(CostLine.charge_period_start, CostLine.sub_account_id)
        .where(CostLine.tenant_id == tenant_id, CostLine.charge_period_start.between(start, end))
        .distinct()
    )
    if account_id is not None:
        recon_q = recon_q.where(Reconciliation.account_id == account_id)
        billed_q = billed_q.where(CostLine.sub_account_id == account_id)
    status: dict[tuple[date, str], str] = {}

    def bump(key: tuple[date, str], value: str) -> None:
        value = value if value in STATUSES else "estimated"
        status[key] = max(status.get(key, value), value, key=STATUSES.index)

    for d, account, value in await session.execute(recon_q):
        bump((as_date(d), account), value)
    for d, account in await session.execute(billed_q):
        bump((as_date(d), account), "billed")
    return status


async def currency(session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date) -> str:
    """The billing currency in range. v1 subscriptions all bill in one currency."""
    for col, day_col in (
        (CostLine.billing_currency, CostLine.charge_period_start),
        (DailyEstimate.currency, DailyEstimate.day),
    ):
        found = (
            await session.scalars(
                select(col)
                .where(col.table.c.tenant_id == tenant_id, day_col.between(start, end))
                .distinct()
            )
        ).all()
        if found:
            return found[0] if len(found) == 1 else DEFAULT_CURRENCY
    return DEFAULT_CURRENCY
