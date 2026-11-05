"""Sync Amazon Bedrock list prices from AWS's public bulk price list into ``Price`` (issue #55).

AWS estimates (today, yesterday) multiply CloudWatch token counts by these prices, the same way
``pricing/retail.py`` feeds Azure and ``pricing/gcp_catalog.py`` feeds GCP. The bulk price
list is public (no AWS credentials), so this runs even before any account is connected. The
table is global list-price data (no tenant). History works like retail: a new effective date
with a different price adds a row, an unchanged price adds nothing, so old estimates stay
reproducible.

``sku_meter`` is the usage type without its region prefix (``Claude3.7Sonnet-input-tokens``),
the same form CUR cost lines carry, and ``region`` the AWS region code. Token prices are
stored per 1M tokens (``unit="1M"``) whatever the offer's unit; per-image, per-hour and other
non-token prices keep the published unit with ``tokens_per_unit`` None, which the estimator
skips.

Coverage (Assumption (spike-aws), from the AWS Price List bulk API docs): Bedrock's own offer
``AmazonBedrock`` (Amazon Nova / Titan and the serverless third-party models AWS lists there)
plus ``AmazonBedrockFoundationModels`` when AWS publishes it. Third-party models sold through
AWS Marketplace ("... (Amazon Bedrock Edition)") are priced in the Marketplace, not the bulk
price list, and their ``MP:`` usage types name no model: they get no estimate price and their
billed lines go to the unmapped queue.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import Price
from ai_cost_tracker.normalise.aws_usage import is_token_priced, parse_usage_type, strip_region
from ai_cost_tracker.normalise.azure_meters import MeterInfo

log = logging.getLogger(__name__)

PRICE_LIST_HOST = "https://pricing.us-east-1.amazonaws.com"
# Assumption (spike-aws): the first offer always exists and its region index is required; the
# second is optional (skipped when AWS answers 403/404 for it).
OFFER_CODES = ("AmazonBedrock", "AmazonBedrockFoundationModels")
_REQUIRED_OFFER = OFFER_CODES[0]
DEFAULT_REGIONS = ("us-east-1",)
CLOUD = "aws"
CURRENCY = "USD"
_PER_MILLION = 1_000_000
_QUANT = Decimal("0.0000000001")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MISSING_STATUSES = frozenset({403, 404})
_MAX_ATTEMPTS = 5
_MAX_SLEEP = 60.0
# Assumption (spike-aws): Bedrock token prices use units like "1K tokens", "1M tokens",
# "Million tokens", "1K input tokens" or plain "Tokens".
_TOKEN_UNIT = re.compile(
    r"^(\d+)?\s*(k|m|thousand|million)?\s*(?:input\s+|output\s+)?tokens?$", re.IGNORECASE
)
_UNIT_SCALE = {None: 1, "k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}
_PREFIXED_MODALITIES = ("audio", "image", "video")


class OfferMissing(Exception):
    """AWS publishes no such offer or region file (403/404 from the price list bucket)."""


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


def region_index_url(offer: str) -> str:
    return f"{PRICE_LIST_HOST}/offers/v1.0/aws/{offer}/current/region_index.json"


async def sync_aws_prices(
    session: AsyncSession,
    http: httpx.AsyncClient | None = None,
    *,
    regions: list[str] | None = None,
    today: date | None = None,
) -> int:
    """Fetch Bedrock offer files for ``regions``, upsert usage types ``parse_usage_type``
    maps; returns rows inserted or updated."""
    today = today or datetime.now(UTC).date()
    wanted = [r.strip().lower() for r in regions or DEFAULT_REGIONS if r.strip()]
    own = http is None
    client = http or httpx.AsyncClient(timeout=120, follow_redirects=True)
    try:
        offers = [f for code in OFFER_CODES for f in await _offer(client, code, wanted)]
    finally:
        if own:
            await client.aclose()
    items = _dedupe(i for region, body in offers for i in parse_offer(body, region, today))
    changed = await _upsert(session, items)
    await session.commit()
    log.info(
        "aws_prices.sync: %d offer files, %d items, %d rows changed",
        len(offers),
        len(items),
        changed,
    )
    return changed


async def _offer(
    client: httpx.AsyncClient, code: str, regions: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    """[(region, offer file)] for the regions this offer publishes."""
    try:
        index = await _get(client, region_index_url(code))
    except OfferMissing:
        if code == _REQUIRED_OFFER:
            raise LookupError(f"AWS price list has no offer {code}") from None
        log.info("aws_prices: offer %s not published; skipped", code)
        return []
    listed = index.get("regions") or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for region in regions:
        path = (listed.get(region) or {}).get("currentVersionUrl")
        if not path:
            log.info("aws_prices: offer %s lists no region %s; skipped", code, region)
            continue
        try:
            out.append((region, await _get(client, PRICE_LIST_HOST + path)))
        except OfferMissing:
            log.warning("aws_prices: %s offer file for %s missing; skipped", code, region)
    return out


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET with retry on throttling / server errors (exponential backoff, Retry-After)."""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = await client.get(url)
        except httpx.TransportError:
            if attempt == _MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_backoff(attempt))
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS - 1:
            await asyncio.sleep(_retry_after(resp) or _backoff(attempt))
            continue
        if resp.status_code in _MISSING_STATUSES:
            raise OfferMissing(url)
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


def parse_offer(body: dict[str, Any], region: str, today: date) -> list[_Item]:
    """Items for every product whose ``usagetype`` parses, priced by its OnDemand terms.

    Assumption (spike-aws): offer file shape per the bulk API docs: ``products`` keyed by SKU
    with ``attributes.usagetype`` / ``regionCode``, and ``terms.OnDemand[sku][term]`` with
    ``effectiveDate`` and ``priceDimensions[*]`` holding ``unit``, ``beginRange`` and
    ``pricePerUnit.USD``. Reserved / provisioned-commitment terms aren't list prices.
    """
    on_demand = (body.get("terms") or {}).get("OnDemand") or {}
    out: list[_Item] = []
    for sku, product in (body.get("products") or {}).items():
        attrs = product.get("attributes") or {}
        usage_type = str(attrs.get("usagetype") or "").strip()
        info = parse_usage_type(usage_type) if usage_type else None
        if info is None:
            continue
        product_region = str(attrs.get("regionCode") or region).strip().lower()
        for term in (on_demand.get(sku) or {}).values():
            rate = _rate(list((term.get("priceDimensions") or {}).values()))
            if rate is None:
                continue
            price, unit = rate
            tokens, multiplier, stored_unit = unit_conversion(unit, is_token_priced(usage_type))
            out.append(
                _Item(
                    sku_meter=strip_region(usage_type),
                    region=product_region,
                    unit=stored_unit,
                    unit_price=(price * multiplier).quantize(_QUANT),
                    currency=CURRENCY,
                    valid_from=_date(term.get("effectiveDate")) or today,
                    ai_model=info.model,
                    ai_direction=price_direction(info),
                    ai_deployment_type=info.deployment_type,
                    tokens_per_unit=tokens,
                )
            )
    return out


def price_direction(info: MeterInfo) -> str:
    """``audio_input`` etc. for non-text modalities, so only text token rates become the
    estimator's base rate (CloudWatch counts all modalities as one number)."""
    if info.modality in _PREFIXED_MODALITIES:
        return f"{info.modality}_{info.direction}"
    return info.direction


def unit_conversion(unit: str, token_priced: bool = True) -> tuple[int | None, Decimal, str]:
    """(tokens_per_unit, price multiplier, stored unit) for an offer's price unit.

    Token units become "1M" (multiplier = 1M / tokens per unit); anything else keeps the
    published unit and no token count.
    """
    m = _TOKEN_UNIT.match(unit.strip()) if token_priced else None
    if not m:
        return None, Decimal(1), unit.strip()
    scale = _UNIT_SCALE[(m.group(2) or "").lower() or None]
    per_unit = int(m.group(1) or 1) * scale
    return _PER_MILLION, Decimal(_PER_MILLION) / Decimal(per_unit), "1M"


def _rate(dimensions: list[dict[str, Any]]) -> tuple[Decimal, str] | None:
    """Lowest tier's non-zero USD price (free tiers come first); 0 when every tier is free."""
    priced: list[tuple[Decimal, Decimal, str]] = []
    for dim in dimensions:
        usd = (dim.get("pricePerUnit") or {}).get(CURRENCY)
        if usd is None:
            continue
        try:
            begin = Decimal(str(dim.get("beginRange") or 0))
            price = Decimal(str(usd))
        except (InvalidOperation, ValueError):
            continue
        priced.append((begin, price, str(dim.get("unit") or "")))
    if not priced:
        return None
    priced.sort(key=lambda t: t[0])
    _, price, unit = next((t for t in priced if t[1] != 0), priced[0])
    return price, unit


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _dedupe(items: Any) -> list[_Item]:
    """One item per (usage type, region, valid_from); the first wins, by offer order."""
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
            continue  # Same price re-published with a new effective date: nothing changed.
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
