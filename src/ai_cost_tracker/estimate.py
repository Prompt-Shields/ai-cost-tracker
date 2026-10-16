"""Estimated cost per deployment and day: token counts x retail price (D6, issue #20).

Billed cost lags by a day or more; estimates fill today and yesterday. Pay-as-you-go pays
retail (ARCHITECTURE.md §4.5), so an estimate should land within ~2% of the billed cost.

Provisioned (PTU) deployments are paid per hour of reserved capacity, not per token, so they
get ``estimated_cost`` 0 with ``price_missing`` False: their cost comes from billing only.

Cached input tokens are priced as regular input: token metrics don't separate them yet.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import DailyEstimate, DeploymentRow, Price, TokenUsageRow
from ai_cost_tracker.normalise.azure_meters import parse_meter

_QUANT = Decimal("0.0000000001")
_TOKEN_DIRECTIONS = ("input", "output")

# Azure deployment SKU name -> deployment type used in meter names.
SKU_DEPLOYMENT_TYPE = {
    "globalstandard": "global",
    "datazonestandard": "data_zone",
    "standard": "regional",
    "globalbatch": "batch",
    "datazonebatch": "batch",
    "provisionedmanaged": "provisioned",
    "globalprovisionedmanaged": "provisioned",
    "datazoneprovisionedmanaged": "provisioned",
}


def normalise_model(name: str | None) -> str | None:
    """Canonical model name, shared by deployment models and meter-parsed models.

    Runs the meter parser on the name, so "gpt-4o-2024-08-06", "GPT 4o" and the meter's
    "gpt-4o" all become "gpt-4o"; unknown families fall back to the lowercased name.
    """
    if not name:
        return None
    info = parse_meter(name)
    return info.model if info else name.strip().lower()


def deployment_type(sku: str | None) -> str:
    """Meter deployment type for a deployment SKU; unknown SKUs are priced as global."""
    return SKU_DEPLOYMENT_TYPE.get((sku or "").replace(" ", "").lower(), "global")


@dataclass
class _Group:
    account_id: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _PriceBook:
    """Per (model, direction, type): {region: [(valid_from, price per token)] newest first}."""

    rows: dict[tuple[str, str, str], dict[str, list[tuple[date, Decimal]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )

    def add(self, p: Price) -> None:
        model = normalise_model(p.ai_model)
        if not (model and p.ai_direction and p.ai_deployment_type and p.tokens_per_unit):
            return
        per_token = Decimal(p.unit_price) / Decimal(p.tokens_per_unit)
        key = (model, p.ai_direction, p.ai_deployment_type)
        self.rows[key][p.region or ""].append((p.valid_from, per_token))

    def finish(self) -> None:
        for regions in self.rows.values():
            for history in regions.values():
                history.sort(reverse=True)

    def lookup(
        self, model: str, direction: str, dtype: str, region: str | None, day: date
    ) -> Decimal | None:
        """Exact region, then any region of the same type, then the global type."""
        for t in (dtype, "global") if dtype != "global" else (dtype,):
            regions = self.rows.get((model, direction, t))
            if not regions:
                continue
            if region and (hit := _at(regions.get(region.lower(), []), day)) is not None:
                return hit
            hits = [h for hist in regions.values() if (h := _at(hist, day)) is not None]
            if hits:
                return max(hits)  # Deterministic; over- rather than under-estimate.
        return None


def _at(history: list[tuple[date, Decimal]], day: date) -> Decimal | None:
    return next((price for valid_from, price in history if valid_from <= day), None)


async def compute_estimates(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> int:
    """Replace DailyEstimate rows for [start, end] (inclusive days, UTC); returns rows written."""
    groups = await _token_groups(session, tenant_id, start, end)
    deployments = await _deployments(session, tenant_id)
    book = await _price_book(session, end)

    await session.execute(
        delete(DailyEstimate).where(
            DailyEstimate.tenant_id == tenant_id,
            DailyEstimate.day >= start,
            DailyEstimate.day <= end,
        )
    )
    for (cloud, resource_id, deployment, day), g in groups.items():
        dep = deployments.get((cloud, resource_id, deployment))
        model = normalise_model(g.model or (dep.model if dep else None))
        dtype = deployment_type(dep.sku if dep else None)
        region = dep.region if dep else None
        costs, missing = _price(book, model, dtype, region, day, g)
        session.add(
            DailyEstimate(
                tenant_id=tenant_id,
                cloud=cloud,
                account_id=g.account_id,
                resource_id=resource_id,
                deployment=deployment,
                model=model,
                day=day,
                input_tokens=g.input_tokens,
                output_tokens=g.output_tokens,
                input_cost=costs["input"],
                output_cost=costs["output"],
                estimated_cost=costs["input"] + costs["output"],
                currency="USD",
                price_missing=missing,
            )
        )
    await session.commit()
    return len(groups)


def _price(
    book: _PriceBook, model: str | None, dtype: str, region: str | None, day: date, g: _Group
) -> tuple[dict[str, Decimal], bool]:
    costs = {d: Decimal(0) for d in _TOKEN_DIRECTIONS}
    if dtype == "provisioned":
        return costs, False
    missing = False
    for direction, tokens in (("input", g.input_tokens), ("output", g.output_tokens)):
        if not tokens:
            continue
        per_token = book.lookup(model, direction, dtype, region, day) if model else None
        if per_token is None:
            missing = True
            continue
        costs[direction] = (per_token * tokens).quantize(_QUANT)
    return costs, missing


async def _token_groups(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> dict[tuple[str, str, str, date], _Group]:
    """Sum hourly tokens per (cloud, resource, deployment, UTC day)."""
    lo = datetime.combine(start, time(), UTC)
    hi = datetime.combine(end + timedelta(days=1), time(), UTC)
    rows = await session.scalars(
        select(TokenUsageRow).where(
            TokenUsageRow.tenant_id == tenant_id,
            # Widened by a day each side: SQLite compares stored strings, so re-check below.
            TokenUsageRow.bucket_start >= lo - timedelta(days=1),
            TokenUsageRow.bucket_start < hi + timedelta(days=1),
        )
    )
    groups: dict[tuple[str, str, str, date], _Group] = {}
    for r in rows:
        ts = r.bucket_start
        ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
        if not lo <= ts < hi:
            continue
        key = (r.cloud, r.resource_id, r.deployment, ts.date())
        g = groups.setdefault(key, _Group(account_id=r.account_id))
        g.model = g.model or r.model
        g.input_tokens += r.input_tokens or 0
        g.output_tokens += r.output_tokens or 0
    return groups


async def _deployments(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict[tuple[str, str, str], DeploymentRow]:
    rows = await session.scalars(select(DeploymentRow).where(DeploymentRow.tenant_id == tenant_id))
    return {(d.cloud, d.resource_id, d.name): d for d in rows}


async def _price_book(session: AsyncSession, end: date) -> _PriceBook:
    """Token prices valid by ``end``. Price is global list-price data, not tenant-scoped."""
    book = _PriceBook()
    rows = await session.scalars(
        select(Price).where(
            Price.cloud == "azure",
            Price.currency == "USD",
            Price.valid_from <= end,
            Price.tokens_per_unit.is_not(None),
            Price.ai_direction.in_(_TOKEN_DIRECTIONS),
        )
    )
    for p in rows:
        book.add(p)
    book.finish()
    return book
