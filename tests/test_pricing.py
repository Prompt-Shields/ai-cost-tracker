import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import select

from ai_cost_tracker.db.models import Price
from ai_cost_tracker.pricing import retail
from ai_cost_tracker.pricing.retail import sync_prices, tokens_per_unit

FIXTURES = Path(__file__).parent / "fixtures" / "prices"


def _pages() -> list[dict]:
    return [json.loads((FIXTURES / f"retail_page{i}.json").read_text()) for i in (1, 2)]


def _client(pages: list[dict], seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        page = pages[1] if "$skip" in request.url.query.decode() else pages[0]
        return httpx.Response(200, json=page)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _prices(session) -> dict[tuple[str, str], list[Price]]:
    out: dict[tuple[str, str], list[Price]] = {}
    for p in await session.scalars(select(Price).order_by(Price.valid_from)):
        out.setdefault((p.sku_meter, p.region), []).append(p)
    return out


def test_tokens_per_unit():
    assert tokens_per_unit("1K") == 1_000
    assert tokens_per_unit("1M") == 1_000_000
    assert tokens_per_unit("1") == 1
    assert tokens_per_unit("1 Hour") is None
    assert tokens_per_unit(None) is None


async def test_sync_follows_pages_and_parses(session):
    seen: list[httpx.Request] = []
    n = await sync_prices(session, _client(_pages(), seen), today=date(2025, 1, 1))
    assert len(seen) == 2
    first = seen[0].url
    assert first.params["api-version"] == "2023-01-01-preview"
    assert first.params["$filter"] == retail.PRICE_FILTER
    assert "$skip" in seen[1].url.query.decode()

    prices = await _prices(session)
    # Reservation and EUR items are dropped; the second tier of a meter is deduplicated.
    assert n == len(prices) == 10
    gin = prices[("gpt-4o-0806-Inp-glbl Tokens", "eastus2")][0]
    assert (gin.ai_model, gin.ai_direction, gin.ai_deployment_type) == ("gpt-4o", "input", "global")
    assert gin.tokens_per_unit == 1_000_000 and gin.unit_price == Decimal("2.5")
    assert gin.valid_from == date(2024, 10, 1)
    gout = prices[("gpt-4o-0806-Outp-glbl Tokens", "eastus2")][0]
    assert gout.tokens_per_unit == 1_000 and gout.unit_price == Decimal("0.01")
    mini_out = prices[("gpt-4o-mini-0718-Outp-glbl Tokens", "eastus2")]
    assert [p.unit_price for p in mini_out] == [Decimal("0.6")]
    cached = prices[("o3-mini-0131-cchd-Inp-glbl Tokens", "swedencentral")][0]
    assert cached.ai_direction == "cached_input"
    ptu = prices[("Provisioned Managed Global Unit", "eastus2")][0]
    # PTU is priced per hour, not per token: mapped to the synthetic PTU model, never used
    # for token estimates.
    assert ptu.tokens_per_unit is None
    assert (ptu.ai_model, ptu.ai_direction) == ("provisioned-throughput", "provisioned")


async def test_sync_is_idempotent(session):
    await sync_prices(session, _client(_pages()))
    before = {
        (p.sku_meter, p.region, p.valid_from, p.unit_price)
        for p in await session.scalars(select(Price))
    }
    assert await sync_prices(session, _client(_pages())) == 0
    after = {
        (p.sku_meter, p.region, p.valid_from, p.unit_price)
        for p in await session.scalars(select(Price))
    }
    assert before == after


async def test_price_change_adds_history_row(session):
    await sync_prices(session, _client(_pages()))
    pages = _pages()
    # Same price republished with a later date: no new row.
    for item in pages[1]["Items"]:
        item["effectiveStartDate"] = "2025-03-01T00:00:00Z"
    # A real price change: new row, old row kept.
    for item in pages[0]["Items"]:
        if item["meterName"] == "gpt-4o-0806-Inp-glbl Tokens":
            item["unitPrice"] = 2.25
            item["effectiveStartDate"] = "2025-03-01T00:00:00Z"
    assert await sync_prices(session, _client(pages)) == 1

    prices = await _prices(session)
    hist = prices[("gpt-4o-0806-Inp-glbl Tokens", "eastus2")]
    assert [(p.valid_from, p.unit_price) for p in hist] == [
        (date(2024, 10, 1), Decimal("2.5")),
        (date(2025, 3, 1), Decimal("2.25")),
    ]
    assert len(prices[("o3-mini-0131-Inp-glbl Tokens", "swedencentral")]) == 1


async def test_retries_on_throttling(session, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(retail.asyncio, "sleep", fake_sleep)
    pages = _pages()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(
            200, json=pages[1] if "$skip" in request.url.query.decode() else pages[0]
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await sync_prices(session, client) == 10
    assert sleeps == [3.0, 2.0]
