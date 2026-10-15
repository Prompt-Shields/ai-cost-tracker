from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker import sync_state
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.db.models import CostLine, SyncState, TokenUsageRow
from ai_cost_tracker.worker import jobs

NOW = datetime(2026, 10, 14, 15, 32, tzinfo=UTC)
SUB = "00000000-0000-0000-0000-00000000demo"


class RecordingConnector(FakeConnector):
    """FakeConnector that logs pulls and can fail one cost month."""

    def __init__(self, fail_month: date | None = None) -> None:
        self.cost_calls: list[tuple[date, date]] = []
        self.usage_calls: list[tuple[datetime, datetime]] = []
        self.fail_month = fail_month

    async def pull_cost(self, account_id, start, end):
        self.cost_calls.append((start, end))
        if start == self.fail_month:
            raise RuntimeError("TooManyRequests")
        return await super().pull_cost(account_id, start, end)

    async def pull_usage(self, account_id, deployments, start, end):
        self.usage_calls.append((start, end))
        return await super().pull_usage(account_id, deployments, start, end)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", connector="fake", default_tenant_slug="internal")


@pytest.fixture
def progress(monkeypatch):
    """Capture every record_backfill call while still writing it."""
    calls: list[tuple[str, date, date | None, float]] = []
    real = sync_state.record_backfill

    async def spy(session, tenant_id, cloud, account_id, source, **kw):
        calls.append((source, kw["backfill_from"], kw["done_through"], kw["progress"]))
        await real(session, tenant_id, cloud, account_id, source, **kw)

    monkeypatch.setattr(sync_state, "record_backfill", spy)
    return calls


async def _state(factory, source) -> SyncState:
    async with factory() as s:
        return await s.scalar(select(SyncState).where(SyncState.source == source))


async def test_backfill_progress_usage_then_cost_newest_first(factory, settings, progress):
    connector = RecordingConnector()
    result = await jobs.run_account_backfill(factory, connector, settings, SUB, now=NOW)
    assert result.failed == [] and result.rows > 0

    usage = [p for p in progress if p[0] == "usage"]
    assert [p[2] for p in usage] == [
        date(2026, 10, 8),
        date(2026, 10, 1),
        date(2026, 9, 24),
        date(2026, 9, 17),
        date(2026, 9, 15),
    ]
    assert {p[1] for p in usage} == {date(2026, 9, 15)}
    assert [round(p[3], 3) for p in usage] == [0.233, 0.467, 0.7, 0.933, 1.0]
    # Usage never asks for the future: the last chunk stops after the current hour.
    assert connector.usage_calls[0][1] == datetime(2026, 10, 14, 16, tzinfo=UTC)

    cost = [p for p in progress if p[0] == "cost"]
    assert [p[2] for p in cost][:3] == [date(2026, 10, 1), date(2026, 9, 1), date(2026, 8, 1)]
    assert cost[-1][2] == date(2025, 11, 1)
    assert {p[1] for p in cost} == {date(2025, 11, 1)}
    assert [p[3] for p in cost] == [pytest.approx((i + 1) / 12) for i in range(12)]
    assert connector.cost_calls[0] == (date(2026, 10, 1), date(2026, 10, 14))
    assert connector.cost_calls[1] == (date(2026, 9, 1), date(2026, 9, 30))

    async with factory() as s:
        oldest_cost = await s.scalar(select(func.min(CostLine.charge_period_start)))
        hours = await s.scalar(select(func.count(func.distinct(TokenUsageRow.bucket_start))))
    assert oldest_cost == date(2025, 11, 1)
    assert hours == 29 * 24 + 16

    state = await _state(factory, "cost")
    assert state.backfill_progress == 1 and state.last_error is None
    assert jobs._utc(state.watermark) == datetime(2026, 10, 14, tzinfo=UTC)


async def test_backfill_is_idempotent(factory, settings, progress):
    await jobs.run_account_backfill(factory, FakeConnector(), settings, SUB, now=NOW)
    async with factory() as s:
        before = (
            await s.scalar(select(func.count(CostLine.id))),
            await s.scalar(select(func.count(TokenUsageRow.id))),
        )
    # A finished backfill is skipped; clearing the progress re-runs it as replaces.
    connector = RecordingConnector()
    await jobs.run_account_backfill(factory, connector, settings, SUB, now=NOW)
    assert connector.cost_calls == [] and connector.usage_calls == []
    async with factory() as s:
        for row in await s.scalars(select(SyncState)):
            row.backfill_done_through = None
        await s.commit()
    await jobs.run_account_backfill(factory, connector, settings, SUB, now=NOW)
    async with factory() as s:
        after = (
            await s.scalar(select(func.count(CostLine.id))),
            await s.scalar(select(func.count(TokenUsageRow.id))),
        )
    assert after == before


async def test_backfill_resumes_from_done_through(factory, settings, progress):
    failing = RecordingConnector(fail_month=date(2026, 7, 1))
    result = await jobs.run_account_backfill(factory, failing, settings, SUB, now=NOW)
    assert result.failed == [SUB]

    state = await _state(factory, "cost")
    assert state.backfill_done_through == date(2026, 8, 1)
    assert float(state.backfill_progress) == pytest.approx(3 / 12, abs=1e-4)
    assert state.last_error == "TooManyRequests"

    progress.clear()
    connector = RecordingConnector()
    result = await jobs.run_account_backfill(factory, connector, settings, SUB, now=NOW)
    assert result.failed == []
    # Usage had finished, so only cost runs, starting at the month that failed.
    assert connector.usage_calls == []
    assert connector.cost_calls[0] == (date(2026, 7, 1), date(2026, 7, 31))
    assert len(connector.cost_calls) == 9
    assert progress[0][2:] == (date(2026, 7, 1), pytest.approx(4 / 12))
    state = await _state(factory, "cost")
    assert state.backfill_done_through == date(2025, 11, 1) and state.last_error is None


async def test_backfill_skips_untracked_account(factory, settings, progress):
    from ai_cost_tracker.db.models import CloudAccountRow

    await jobs.run_inventory(factory, FakeConnector(), settings)
    async with factory() as s:
        row = await s.scalar(select(CloudAccountRow))
        row.tracked = False
        await s.commit()
    connector = RecordingConnector()
    result = await jobs.run_account_backfill(factory, connector, settings, SUB, now=NOW)
    assert result.rows == 0 and connector.cost_calls == [] and progress == []
