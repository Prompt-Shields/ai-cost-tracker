"""Sync Vertex AI list prices from the Cloud Billing Catalog API into ``Price`` (issue #49).

GCP estimates (today, yesterday) multiply Cloud Monitoring token counts by these prices, the
same way ``pricing/retail.py`` feeds Azure estimates. Prices are public list prices in USD,
so the table is global (no tenant). History works like retail: a new effective date with a
different price adds a row, an unchanged price adds nothing, so old estimates stay
reproducible.

Token prices are stored per 1M tokens (``unit="1M"``) whatever the catalog's usage unit, so
sub-nano per-token prices keep their precision in ``Numeric(20, 10)``. Per-character, per-image
and per-second SKUs are stored as published with ``tokens_per_unit`` None, which the estimator
skips.

Response shapes follow Google's public REST reference for ``services.list`` and
``services.skus.list`` (Assumption (spike-gcp): not yet checked against a live catalog).
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import Price
from ai_cost_tracker.normalise.azure_meters import MeterInfo
from ai_cost_tracker.normalise.gcp_skus import is_token_priced, parse_sku

log = logging.getLogger(__name__)

CATALOG_URL = "https://cloudbilling.googleapis.com/v1"
# Assumption (spike-gcp): Gemini and other first-party Vertex AI SKUs sit under the service
# whose displayName is "Vertex AI". Partner models (Claude, Llama, Mistral) may be billed as
# separate Marketplace services; their SKUs parse, but are only synced if listed here.
SERVICE_DISPLAY_NAMES = ("Vertex AI",)
CLOUD = "gcp"
CURRENCY = "USD"
PAGE_SIZE = 5000
_MAX_PAGES = 200
_PER_MILLION = 1_000_000
_QUANT = Decimal("0.0000000001")
_NANOS = Decimal(1_000_000_000)
# UCUM-style usage units for token counts: "count", "{token}", "k{token}", "1M{token}", ...
_TOKEN_UNIT = re.compile(r"^(\d+)?\s*(k|K|m|M|Mi)?\s*\{?(count|tokens?)\}?$")
_UNIT_SCALE = {None: 1, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000, "Mi": 1_000_000}
# Modalities priced apart from text: token metrics count all modalities as one number, so these
# rows must never match the estimator's plain "input"/"output" lookup.
_PREFIXED_MODALITIES = ("audio", "image", "video")


class CatalogClient(Protocol):
    """The slice of ``connectors.gcp.client.GcpClient`` this module needs."""

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...


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


async def sync_gcp_prices(
    session: AsyncSession, client: CatalogClient, *, today: date | None = None
) -> int:
    """Fetch Vertex AI SKUs, upsert the ones ``parse_sku`` maps; returns rows inserted or
    updated."""
    today = today or datetime.now(UTC).date()
    service_ids = await find_services(client)
    skus: list[dict[str, Any]] = []
    for service_id in service_ids:
        skus.extend(await list_skus(client, service_id))
    items = _dedupe(i for raw in skus for i in parse_catalog_sku(raw, today))
    changed = await _upsert(session, items)
    await session.commit()
    log.info("gcp_prices.sync: %d skus, %d items, %d rows changed", len(skus), len(items), changed)
    return changed


async def find_services(client: CatalogClient) -> list[str]:
    """Service ids (e.g. ``C7E2-9256-1C43``) whose displayName is in SERVICE_DISPLAY_NAMES."""
    services = await _paged(client, f"{CATALOG_URL}/services", "services", {})
    ids = [
        s.get("serviceId") or str(s.get("name", "")).rsplit("/", 1)[-1]
        for s in services
        if s.get("displayName") in SERVICE_DISPLAY_NAMES
    ]
    ids = [i for i in ids if i]
    if not ids:
        raise LookupError(f"Cloud Billing catalog lists no service named {SERVICE_DISPLAY_NAMES}")
    return ids


async def list_skus(client: CatalogClient, service_id: str) -> list[dict[str, Any]]:
    """All SKUs of one service, priced in USD."""
    url = f"{CATALOG_URL}/services/{service_id}/skus"
    return await _paged(client, url, "skus", {"currencyCode": CURRENCY})


async def _paged(
    client: CatalogClient, url: str, key: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Follow ``nextPageToken`` (retries live in the client)."""
    out: list[dict[str, Any]] = []
    token = ""
    for _ in range(_MAX_PAGES):
        query = {**params, "pageSize": PAGE_SIZE}
        if token:
            query["pageToken"] = token
        body = await client.get(url, query)
        out.extend(body.get(key) or [])
        token = body.get("nextPageToken") or ""
        if not token:
            return out
    raise RuntimeError(f"Cloud Billing catalog returned too many pages for {url}")


def parse_catalog_sku(raw: dict[str, Any], today: date) -> list[_Item]:
    """One item per (service region, pricingInfo entry); [] for unmapped or non-USD SKUs."""
    description = (raw.get("description") or "").strip()
    info = parse_sku(description) if description else None
    if info is None:
        return []
    usage_type = (raw.get("category") or {}).get("usageType") or "OnDemand"
    if usage_type != "OnDemand":
        return []  # Committed-use and promotional SKUs aren't the pay-as-you-go list price.
    regions = sorted({_region(r) for r in raw.get("serviceRegions") or []}) or [""]
    token_priced = is_token_priced(description)
    out: list[_Item] = []
    for pricing in raw.get("pricingInfo") or []:
        expr = pricing.get("pricingExpression") or {}
        rate = _rate(expr.get("tieredRates") or [])
        if rate is None:
            continue
        price, currency = rate
        if currency != CURRENCY:
            continue
        unit, tokens, multiplier = unit_conversion(expr, token_priced)
        valid_from = _date(pricing.get("effectiveTime")) or today
        for region in regions:
            out.append(
                _Item(
                    sku_meter=description,
                    region=region,
                    unit=unit,
                    unit_price=(price * multiplier).quantize(_QUANT),
                    currency=CURRENCY,
                    valid_from=valid_from,
                    ai_model=info.model,
                    ai_direction=price_direction(info),
                    ai_deployment_type=info.deployment_type,
                    tokens_per_unit=tokens,
                )
            )
    return out


def price_direction(info: MeterInfo) -> str:
    """``image_input`` etc. for non-text modalities; text and unlabelled SKUs keep the parsed
    direction, so only text token rates become the estimator's base rate."""
    if info.modality in _PREFIXED_MODALITIES:
        return f"{info.modality}_{info.direction}"
    return info.direction


def unit_conversion(
    expr: dict[str, Any], token_priced: bool = True
) -> tuple[str, int | None, Decimal]:
    """(stored unit, tokens_per_unit, price multiplier) for a ``pricingExpression``.

    Token units are normalised to "1M" (multiplier = 1M / tokens per usage unit). Anything else
    (characters, images, seconds, hours) keeps the catalog's usage unit and no token count.
    Assumption (spike-gcp): Gemini token SKUs use a count-like ``usageUnit`` ("count",
    "k{token}", "1M{token}") or a count ``baseUnit`` with ``baseUnitConversionFactor``.
    """
    usage = str(expr.get("usageUnit") or "").strip()
    described = f"{usage} {expr.get('usageUnitDescription') or ''}".lower()
    if not token_priced or "char" in described:
        return usage, None, Decimal(1)
    per_unit = _tokens_per_usage_unit(usage)
    if per_unit is None and _TOKEN_UNIT.match(str(expr.get("baseUnit") or "").strip()):
        per_unit = _positive_int(expr.get("baseUnitConversionFactor"))
    if per_unit is None:
        return usage, None, Decimal(1)
    return "1M", _PER_MILLION, Decimal(_PER_MILLION) / Decimal(per_unit)


def _tokens_per_usage_unit(usage: str) -> int | None:
    m = _TOKEN_UNIT.match(usage)
    if not m:
        return None
    return int(m.group(1) or 1) * _UNIT_SCALE[m.group(2)]


def _positive_int(value: Any) -> int | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _rate(tiers: list[dict[str, Any]]) -> tuple[Decimal, str] | None:
    """First non-zero tier's unit price (free tiers come first); 0 when every tier is free."""
    priced: list[tuple[Decimal, Decimal, str]] = []
    for tier in tiers:
        money = tier.get("unitPrice") or {}
        try:
            start = Decimal(str(tier.get("startUsageAmount") or 0))
            price = (
                Decimal(str(money.get("units") or 0)) + Decimal(money.get("nanos") or 0) / _NANOS
            )
        except (InvalidOperation, ValueError):
            continue
        priced.append((start, price, money.get("currencyCode") or CURRENCY))
    if not priced:
        return None
    priced.sort(key=lambda t: t[0])
    _, price, currency = next((t for t in priced if t[1] != 0), priced[0])
    return price, currency


def _region(value: str) -> str:
    region = str(value).strip().lower()
    return "" if region == "global" else region


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _dedupe(items: Any) -> list[_Item]:
    """One item per (description, region, valid_from): distinct SKU ids can share a
    description (e.g. per geo taxonomy); the first wins, deterministically by catalog order."""
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
    """Same history rules as ``pricing.retail``: replace a same-day row that differs, add a
    row only when the price or unit changed since the latest earlier one."""
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
            continue  # Same price re-published with a new effective time: nothing changed.
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
