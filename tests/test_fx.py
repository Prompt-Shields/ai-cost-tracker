import logging
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from ai_cost_tracker.db.models import FxRate
from ai_cost_tracker.fx import (
    DEMO_EUR_PER_USD,
    ECB_90D_URL,
    ECB_HIST_URL,
    FxBook,
    FxError,
    load_fx_book,
    parse_ecb_xml,
    seed_demo_fx,
    sync_fx_rates,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "fx" / "eurofxref-hist-90d.xml").read_bytes()
D = date(2025, 3, 6)


def _book() -> FxBook:
    return FxBook(
        [
            (date(2025, 3, 5), "EUR", "USD", Decimal("1.08")),
            (date(2025, 3, 6), "EUR", "USD", Decimal("1.10")),
            (date(2025, 3, 6), "EUR", "GBP", Decimal("0.85")),
        ]
    )


def test_same_currency_is_identity():
    assert FxBook().rate(D, "usd", "USD") == (Decimal(1), False)
    assert FxBook().convert(Decimal("1.23"), D, "EUR", "EUR") == (Decimal("1.23"), False)


def test_direct_and_inverse_rates():
    book = _book()
    assert book.rate(D, "EUR", "USD") == (Decimal("1.10"), False)
    rate, approx = book.rate(D, "USD", "EUR")
    assert not approx and rate * Decimal("1.10") == pytest.approx(Decimal(1))
    assert book.convert(Decimal("11"), D, "USD", "EUR") == (Decimal("10.0000000000"), False)


def test_cross_rate_goes_through_eur():
    rate, approx = _book().rate(D, "USD", "GBP")
    assert not approx
    assert rate == Decimal("0.85") / Decimal("1.10")
    amount, _ = _book().convert(Decimal("110"), D, "USD", "GBP")
    assert amount == Decimal("85.0000000000")


def test_weekend_uses_latest_earlier_rate():
    book = _book()
    assert book.rate(date(2025, 3, 9), "EUR", "USD") == (Decimal("1.10"), False)
    assert book.rate(date(2025, 3, 5), "EUR", "USD") == (Decimal("1.08"), False)


def test_approximate_when_stale_or_from_the_future():
    book = _book()
    assert book.rate(date(2025, 3, 13), "EUR", "USD") == (Decimal("1.10"), False)  # 7 days
    assert book.rate(date(2025, 3, 14), "EUR", "USD") == (Decimal("1.10"), True)  # 8 days
    assert book.rate(date(2025, 3, 1), "EUR", "USD") == (Decimal("1.08"), True)  # later rate
    # One stale leg makes the cross rate approximate.
    _, approx = book.rate(date(2025, 3, 4), "USD", "GBP")
    assert approx


def test_missing_rate_leaves_amount_and_logs_once(caplog):
    book = _book()
    assert book.rate(D, "USD", "JPY") is None
    with caplog.at_level(logging.WARNING, logger="ai_cost_tracker.fx"):
        assert book.convert(Decimal("5"), D, "USD", "JPY") == (Decimal("5"), True)
        assert book.convert(Decimal("6"), D, "USD", "JPY") == (Decimal("6"), True)
    assert len([r for r in caplog.records if "JPY" in r.getMessage()]) == 1


def test_parse_ecb_xml():
    rows = parse_ecb_xml(FIXTURE)
    assert (date(2025, 3, 7), "USD", Decimal("1.0807000000")) in rows
    assert (date(2025, 3, 5), "GBP", Decimal("0.8353000000")) in rows
    assert len(rows) == 8  # the bogus JPY rate is skipped
    assert rows == sorted(rows)


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
        b"<!doctype html><html></html>",
        b"not xml at all",
    ],
)
def test_parse_rejects_unsafe_or_invalid(body):
    with pytest.raises(FxError):
        parse_ecb_xml(body)


def _client(calls: list[str], statuses: list[int] | None = None) -> httpx.AsyncClient:
    pending = list(statuses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if pending:
            return httpx.Response(pending.pop(0), headers={"retry-after": "0"})
        return httpx.Response(200, content=FIXTURE)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_sync_upserts_full_history_when_empty_then_90d(session, monkeypatch):
    calls: list[str] = []
    assert await sync_fx_rates(session, _client(calls)) == 8
    assert calls == [ECB_HIST_URL]
    rows = list(await session.scalars(select(FxRate)))
    assert (
        len(rows) == 8 and {r.base for r in rows} == {"EUR"} and {r.source for r in rows} == {"ecb"}
    )

    # Later syncs use the 90-day feed and only keep recent days (fixture days are old).
    assert await sync_fx_rates(session, _client(calls), days=5000) == 8
    assert calls[-1] == ECB_HIST_URL  # days > 90 needs the full history
    assert await sync_fx_rates(session, _client(calls)) == 0
    assert calls[-1] == ECB_90D_URL
    assert len(list(await session.scalars(select(FxRate)))) == 8


async def test_sync_updates_changed_rate_in_place(session):
    session.add(
        FxRate(day=date(2025, 3, 7), base="EUR", quote="USD", rate=Decimal("2"), source="demo")
    )
    await session.commit()
    await sync_fx_rates(session, _client([]), days=5000)
    row = await session.scalar(
        select(FxRate).where(FxRate.day == date(2025, 3, 7), FxRate.quote == "USD")
    )
    await session.refresh(row)
    assert row.rate == Decimal("1.0807") and row.source == "ecb"
    assert len(list(await session.scalars(select(FxRate)))) == 8


async def test_sync_retries_server_errors(session, monkeypatch):
    monkeypatch.setattr("ai_cost_tracker.fx.asyncio.sleep", _no_sleep)
    calls: list[str] = []
    assert await sync_fx_rates(session, _client(calls, [503, 429])) == 8
    assert len(calls) == 3


async def test_sync_raises_on_client_error(session):
    with pytest.raises(httpx.HTTPStatusError):
        await sync_fx_rates(session, _client([], [404]))


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_seed_demo_fx_is_idempotent(session):
    start, end = date(2025, 3, 1), date(2025, 3, 10)
    assert await seed_demo_fx(session, start, end) == 10
    assert await seed_demo_fx(session, start, end) == 10
    rows = list(await session.scalars(select(FxRate).order_by(FxRate.day)))
    assert len(rows) == 10
    assert {(r.base, r.quote, r.source) for r in rows} == {("EUR", "USD", "demo")}
    assert {r.rate for r in rows} == {Decimal("1.0869565217")}
    # The demo rate converts a USD amount to EUR at DEMO_EUR_PER_USD.
    book = await load_fx_book(session, start, end)
    eur, approx = book.convert(Decimal("100"), date(2025, 3, 5), "USD", "EUR")
    assert not approx
    assert abs(eur - Decimal(100) * DEMO_EUR_PER_USD) < Decimal("0.000001")


async def test_load_fx_book_window_and_older_fallback(session):
    session.add_all(
        [
            FxRate(day=date(2024, 12, 1), base="EUR", quote="USD", rate=Decimal("1.05")),
            FxRate(day=date(2024, 11, 1), base="EUR", quote="USD", rate=Decimal("1.01")),
            FxRate(day=date(2025, 3, 3), base="EUR", quote="USD", rate=Decimal("1.08")),
        ]
    )
    await session.commit()
    book = await load_fx_book(session, date(2025, 3, 1), date(2025, 3, 5))
    # Outside the 14-day lookback only the latest older row is kept, flagged approximate.
    assert book.rate(date(2025, 3, 1), "EUR", "USD") == (Decimal("1.05"), True)
    assert book.rate(date(2025, 3, 4), "EUR", "USD") == (Decimal("1.08"), False)
    empty = await load_fx_book(session, date(2025, 3, 1) - timedelta(days=200), date(2024, 1, 1))
    assert empty.rate(date(2024, 1, 1), "EUR", "USD") is None
