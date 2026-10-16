"""Sync AI meter prices from the public Azure Retail Prices API into ``Price``.

Pay-as-you-go subscriptions pay list price (ARCHITECTURE.md §4.5), so these rows are the real
unit prices that ``estimate.py`` multiplies token counts by. The table is global (public list
prices, not tenant data). History is kept via ``valid_from``: a new effective date with a
different price adds a row, an unchanged price adds nothing, so old estimates stay reproducible.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import Price
from ai_cost_tracker.normalise.azure_meters import MeterInfo, parse_meter

log = logging.getLogger(__name__)

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"

# Server-side filter: AI products, consumption prices, USD.
# Assumption (issue #2, unverified): Azure OpenAI / Foundry model meters are listed under
# serviceName 'Cognitive Services' (older) or 'Foundry Models' (newer) with a productName
# containing 'OpenAI' or 'Foundry', and the API honours contains() and currencyCode in $filter.
# Items are re-checked client-side, so a too-broad filter only costs bandwidth.
PRICE_FILTER = (
    "priceType eq 'Consumption' and currencyCode eq 'USD' and "
    "(serviceName eq 'Cognitive Services' or serviceName eq 'Foundry Models') and "
    "(contains(productName, 'OpenAI') or contains(productName, 'Foundry'))"
)

CLOUD = "azure"
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_MAX_SLEEP = 60.0
_MAX_PAGES = 500

_UNITS = {"1": 1, "1k": 1_000, "1m": 1_000_000}


@dataclass(frozen=True)
class _Item:
    sku_meter: str
    region: str
    unit: str
    unit_price: Decimal
    currency: str
    valid_from: date
    ai_model: str | None
    ai_direction: str | None
    ai_deployment_type: str | None
    tokens_per_unit: int | None


def tokens_per_unit(unit_of_measure: str | None) -> int | None:
    """Tokens covered by one unit: "1K" -> 1000, "1M" -> 1_000_000, "1" -> 1, else None.

    Anything else (e.g. "1 Hour", "100") is not a per-token price and is skipped for estimates.
    """
    if not unit_of_measure:
        return None
    return _UNITS.get(unit_of_measure.strip().lower().replace(" ", ""))


async def sync_prices(
    session: AsyncSession, http: httpx.AsyncClient | None = None, today: date | None = None
) -> int:
    """Fetch all AI consumption prices and upsert them; returns rows inserted or updated."""
    today = today or datetime.now(UTC).date()
    own = http is None
    client = http or httpx.AsyncClient(timeout=60)
    try:
        raw = await _fetch_all(client)
    finally:
        if own:
            await client.aclose()
    items = _dedupe(i for r in raw if (i := _parse_item(r, today)) is not None)
    changed = await _upsert(session, items)
    await session.commit()
    log.info("prices.sync: %d items, %d rows changed", len(items), changed)
    return changed


async def _fetch_all(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Follow ``NextPageLink`` until the last page."""
    url: str | None = RETAIL_PRICES_URL
    params: dict[str, str] | None = {"api-version": API_VERSION, "$filter": PRICE_FILTER}
    out: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        if not url:
            return out
        body = await _get(client, url, params)
        out.extend(body.get("Items") or [])
        url = body.get("NextPageLink")
        params = None  # NextPageLink already carries the query string.
    raise RuntimeError("Retail Prices API returned too many pages")


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, str] | None) -> dict:
    """GET with retry on throttling / server errors (exponential backoff, Retry-After honoured)."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.get(url, params=params)
        except httpx.TransportError:
            if attempt == _MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_backoff(attempt))
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_retry_after(resp) or _backoff(attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def _backoff(attempt: int) -> float:
    return min(2.0**attempt, _MAX_SLEEP)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(float(resp.headers["retry-after"]), _MAX_SLEEP)
    except (KeyError, ValueError):
        return None


def _parse_item(raw: dict[str, Any], today: date) -> _Item | None:
    """Map one API item to a price row; None for non-consumption, non-USD or bad prices."""
    if raw.get("type", "Consumption") != "Consumption":
        return None
    if (raw.get("currencyCode") or "USD") != "USD":
        return None
    meter = raw.get("meterName") or ""
    if not meter:
        return None
    try:
        price = Decimal(str(raw["unitPrice"]))
    except (KeyError, InvalidOperation):
        return None
    # Some products put the informative name in skuName; fall back to it for parsing.
    info = parse_meter(meter) or parse_meter(raw.get("skuName") or "")
    unit = raw.get("unitOfMeasure") or ""
    return _Item(
        sku_meter=meter,
        region=(raw.get("armRegionName") or "").lower(),
        unit=unit,
        unit_price=price,
        currency="USD",
        valid_from=_date(raw.get("effectiveStartDate")) or today,
        ai_model=info.model if info else None,
        ai_direction=_direction(info),
        ai_deployment_type=info.deployment_type if info else None,
        tokens_per_unit=tokens_per_unit(unit),
    )


def _direction(info: MeterInfo | None) -> str | None:
    """Meter direction, prefixed for audio/image meters ("audio_input").

    Token metrics only count text tokens, so audio/image prices must never match a plain
    "input"/"output" lookup in the estimator.
    """
    if info is None:
        return None
    modality = getattr(info, "modality", None)
    return f"{modality}_{info.direction}" if modality in ("audio", "image") else info.direction


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _dedupe(items: Any) -> list[_Item]:
    """One item per (meter, region, valid_from); the API can repeat a meter across tiers."""
    out: dict[tuple[str, str, date], _Item] = {}
    for i in items:
        out.setdefault((i.sku_meter, i.region, i.valid_from), i)
    return list(out.values())


_FIELDS = (
    "unit",
    "unit_price",
    "currency",
    "ai_model",
    "ai_direction",
    "ai_deployment_type",
    "tokens_per_unit",
)


async def _upsert(session: AsyncSession, items: list[_Item]) -> int:
    existing: dict[tuple[str, str], list[Price]] = {}
    for row in await session.scalars(select(Price).where(Price.cloud == CLOUD)):
        existing.setdefault((row.sku_meter, row.region), []).append(row)

    changed = 0
    for item in sorted(items, key=lambda i: i.valid_from):
        history = existing.setdefault((item.sku_meter, item.region), [])
        same_day = next((r for r in history if r.valid_from == item.valid_from), None)
        if same_day is not None:
            if any(getattr(same_day, f) != getattr(item, f) for f in _FIELDS):
                for f in _FIELDS:
                    setattr(same_day, f, getattr(item, f))
                changed += 1
            continue
        earlier = [r for r in history if r.valid_from < item.valid_from]
        latest = max(earlier, key=lambda r: r.valid_from, default=None)
        if latest is not None and (latest.unit_price, latest.unit) == (item.unit_price, item.unit):
            continue  # Same price re-published with a new date: nothing changed.
        row = Price(
            cloud=CLOUD,
            sku_meter=item.sku_meter,
            region=item.region,
            valid_from=item.valid_from,
            **{f: getattr(item, f) for f in _FIELDS},
        )
        session.add(row)
        history.append(row)
        changed += 1
    return changed
