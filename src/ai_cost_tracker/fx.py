"""Daily FX rates (ECB reference rates) and conversion between billing currencies (M3, #50).

Clouds bill in the account's currency (a GCP project can bill in EUR while Azure bills in USD),
so totals across clouds and estimate-vs-bill comparisons need a common currency. The ECB
publishes EUR-based reference rates on TARGET business days; every other pair is crossed
through EUR, and weekends/holidays use the latest earlier rate. Rows are public market data,
like ``Price``, so they are not tenant-scoped.
"""

import asyncio
import bisect
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import FxRate

try:  # Shared with the fake GCP connector so demo EUR bills reconcile with demo rates.
    from ai_cost_tracker.pricing.demo_gcp import DEMO_EUR_PER_USD
except ImportError:  # pragma: no cover - demo_gcp lands separately in M3.
    DEMO_EUR_PER_USD = Decimal("0.92")

log = logging.getLogger(__name__)

ECB_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
EUR = "EUR"
STALE_AFTER = timedelta(days=7)  # Longer than any TARGET holiday gap.
LOOKBACK = timedelta(days=14)
_QUANT = Decimal("0.0000000001")  # 10 dp, matches Numeric(20, 10) / Money scale.
_CCY = re.compile(r"^[A-Z]{3}$")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 5
_MAX_SLEEP = 60.0
_CHUNK = 1000


class FxError(ValueError):
    """The ECB feed could not be parsed safely."""


class FxBook:
    """In-memory rates for a date range; ``rate``/``convert`` never touch the database."""

    def __init__(self, rows: list[tuple[date, str, str, Decimal]] | None = None) -> None:
        # (base, quote) -> sorted days and their rates.
        self._days: dict[tuple[str, str], list[date]] = {}
        self._rates: dict[tuple[str, str], list[Decimal]] = {}
        self._warned: set[tuple[str, str]] = set()
        for day, base, quote, rate in sorted(rows or []):
            if rate and rate > 0:
                self._days.setdefault((base, quote), []).append(day)
                self._rates.setdefault((base, quote), []).append(Decimal(rate))

    def rate(self, day: date, from_ccy: str, to_ccy: str) -> tuple[Decimal, bool] | None:
        """Units of ``to_ccy`` per one ``from_ccy`` on ``day`` and whether it is approximate.

        Approximate when a leg's rate is more than 7 days older than ``day`` or comes from
        after ``day`` (no earlier rate known). None when either currency has no rate at all.
        """
        from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
        if from_ccy == to_ccy:
            return Decimal(1), False
        from_leg = self._eur_to(day, from_ccy)
        to_leg = self._eur_to(day, to_ccy)
        if from_leg is None or to_leg is None:
            return None
        return to_leg[0] / from_leg[0], from_leg[1] or to_leg[1]

    def convert(
        self, amount: Decimal, day: date, from_ccy: str, to_ccy: str
    ) -> tuple[Decimal, bool]:
        """(amount in ``to_ccy`` quantized to 10 dp, approximate); unchanged + True if no rate."""
        if from_ccy.upper() == to_ccy.upper():
            return Decimal(amount), False
        found = self.rate(day, from_ccy, to_ccy)
        if found is None:
            pair = (from_ccy.upper(), to_ccy.upper())
            if pair not in self._warned:
                self._warned.add(pair)
                log.warning("fx: no rate for %s->%s, amounts left unconverted", *pair)
            return Decimal(amount), True
        rate, approximate = found
        return (Decimal(amount) * rate).quantize(_QUANT), approximate

    def _eur_to(self, day: date, ccy: str) -> tuple[Decimal, bool] | None:
        """Units of ``ccy`` per EUR, from a direct EUR->ccy row or the inverse of ccy->EUR."""
        if ccy == EUR:
            return Decimal(1), False
        found = self._lookup((EUR, ccy), day)
        if found is not None:
            return found
        inverse = self._lookup((ccy, EUR), day)
        if inverse is not None:
            return Decimal(1) / inverse[0], inverse[1]
        return None

    def _lookup(self, pair: tuple[str, str], day: date) -> tuple[Decimal, bool] | None:
        days = self._days.get(pair)
        if not days:
            return None
        i = bisect.bisect_right(days, day) - 1
        if i < 0:  # Nothing on or before the day: borrow the first later rate.
            return self._rates[pair][0], True
        return self._rates[pair][i], day - days[i] > STALE_AFTER


async def load_fx_book(session: AsyncSession, start: date, end: date) -> FxBook:
    """Rates for [start - 14 days, end], plus each pair's latest rate before that window.

    The extra row keeps a long feed outage convertible (flagged approximate) instead of
    leaving amounts unconverted.
    """
    lo = start - LOOKBACK
    rows = list(
        (
            await session.execute(
                select(FxRate.day, FxRate.base, FxRate.quote, FxRate.rate).where(
                    FxRate.day >= lo, FxRate.day <= end
                )
            )
        ).all()
    )
    latest = (
        select(FxRate.base, FxRate.quote, func.max(FxRate.day).label("day"))
        .where(FxRate.day < lo)
        .group_by(FxRate.base, FxRate.quote)
        .subquery()
    )
    rows += list(
        (
            await session.execute(
                select(FxRate.day, FxRate.base, FxRate.quote, FxRate.rate).join(
                    latest,
                    (FxRate.base == latest.c.base)
                    & (FxRate.quote == latest.c.quote)
                    & (FxRate.day == latest.c.day),
                )
            )
        ).all()
    )
    return FxBook([(d, b, q, Decimal(r)) for d, b, q, r in rows])


async def sync_fx_rates(
    session: AsyncSession, http: httpx.AsyncClient | None = None, *, days: int = 90
) -> int:
    """Fetch ECB reference rates and upsert them; returns rows written.

    An empty table (or ``days`` > 90) pulls the full history so old cost days convert too;
    otherwise the 90-day feed, keeping only the last ``days`` days.
    """
    empty = await session.scalar(select(func.count()).select_from(FxRate)) == 0
    full = empty or days > 90
    own = http is None
    client = http or httpx.AsyncClient(timeout=60)
    try:
        body = await _get(client, ECB_HIST_URL if full else ECB_90D_URL)
    finally:
        if own:
            await client.aclose()
    rows = parse_ecb_xml(body)
    if not empty:
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        rows = [r for r in rows if r[0] >= cutoff]
    values = [{"day": d, "base": EUR, "quote": q, "rate": r, "source": "ecb"} for d, q, r in rows]
    await _upsert(session, values)
    await session.commit()
    log.info("fx.sync: %d rows", len(values))
    return len(values)


async def seed_demo_fx(session: AsyncSession, start: date, end: date) -> int:
    """EUR->USD at the demo rate for every day in [start, end]; idempotent upsert.

    Matches ``DEMO_EUR_PER_USD`` used by the fake GCP connector, so its EUR bills reconcile.
    """
    rate = (Decimal(1) / DEMO_EUR_PER_USD).quantize(_QUANT)
    values = [
        {
            "day": start + timedelta(days=i),
            "base": EUR,
            "quote": "USD",
            "rate": rate,
            "source": "demo",
        }
        for i in range((end - start).days + 1)
    ]
    await _upsert(session, values)
    await session.commit()
    return len(values)


def parse_ecb_xml(body: bytes) -> list[tuple[date, str, Decimal]]:
    """(day, quote currency, units per EUR) from an eurofxref XML document.

    ElementTree expands internal entities, so documents with a DOCTYPE (the only place
    entities can be declared) are rejected before parsing; the real feed has none.
    """
    head = body.lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise FxError("ECB feed contains a DOCTYPE/ENTITY declaration; refusing to parse")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FxError(f"ECB feed is not valid XML: {exc}") from exc
    out: dict[tuple[date, str], Decimal] = {}
    for cube in root.iter():
        if not cube.tag.endswith("Cube") or "time" not in cube.attrib:
            continue
        try:
            day = date.fromisoformat(cube.attrib["time"])
        except ValueError:
            continue
        for child in cube:
            ccy = (child.attrib.get("currency") or "").upper()
            try:
                rate = Decimal(child.attrib.get("rate", ""))
            except InvalidOperation:
                continue
            if _CCY.match(ccy) and rate.is_finite() and rate > 0:
                out[(day, ccy)] = rate.quantize(_QUANT)
    return [(d, q, r) for (d, q), r in sorted(out.items())]


async def _upsert(session: AsyncSession, values: list[dict[str, Any]]) -> None:
    """Insert-or-update on (day, base, quote), so re-syncs and concurrent jobs can't collide."""
    if not values:
        return
    dialect = (await session.connection()).dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise NotImplementedError(f"fx upsert not supported on {dialect}")
    module = postgresql if dialect == "postgresql" else sqlite
    for i in range(0, len(values), _CHUNK):
        stmt = module.insert(FxRate).values(values[i : i + _CHUNK])
        stmt = stmt.on_conflict_do_update(
            index_elements=["day", "base", "quote"],
            set_={c: getattr(stmt.excluded, c) for c in ("rate", "source")},
        )
        await session.execute(stmt)


async def _get(client: httpx.AsyncClient, url: str) -> bytes:
    """GET with retry on throttling / server errors (exponential backoff, Retry-After honoured)."""
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
        resp.raise_for_status()
        return resp.content
    raise RuntimeError("unreachable")


def _backoff(attempt: int) -> float:
    return min(2.0**attempt, _MAX_SLEEP)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(float(resp.headers["retry-after"]), _MAX_SLEEP)
    except (KeyError, ValueError):
        return None
