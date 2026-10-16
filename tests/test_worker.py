from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage
from ai_cost_tracker.db.models import CloudAccountRow, CostLine, SyncState, TokenUsageRow
from ai_cost_tracker.worker import jobs

NOW = datetime(2026, 10, 14, 15, 32, tzinfo=UTC)


class TwoAccountConnector(FakeConnector):
    """Fake data for a good and a bad subscription; the bad one fails cost and usage pulls."""

    async def list_accounts(self) -> list[CloudAccount]:
        return [
            CloudAccount(cloud="azure", account_id=a, name=a, has_ai_resources=True)
            for a in ("sub-bad", "sub-good")
        ]

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        deps = await super().list_deployments(account_id)
        return [d.model_copy(update={"resource_id": f"{account_id}{d.resource_id}"}) for d in deps]

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        if account_id == "sub-bad":
            raise RuntimeError("AuthorizationFailed")
        return await super().pull_cost(account_id, start, end)

    async def pull_usage(self, account_id, deployments, start, end) -> list[TokenUsage]:
        if account_id == "sub-bad":
            raise RuntimeError("AuthorizationFailed")
        return await super().pull_usage(account_id, deployments, start, end)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", connector="fake", default_tenant_slug="internal")


async def _count(factory, column, *where) -> int:
    async with factory() as s:
        return await s.scalar(select(func.count(column)).where(*where))


async def test_run_once_writes_inventory_cost_and_usage(factory, settings):
    result = await jobs.run_once(factory, FakeConnector(), settings, now=NOW)

    assert result.failed == []
    assert result.rows > 0
    assert await _count(factory, CloudAccountRow.id) == 1
    assert await _count(factory, CostLine.id) > 0
    # Three deployments x three hourly buckets (two-hour window plus the current hour).
    assert await _count(factory, TokenUsageRow.id) == 9


async def test_run_once_is_idempotent(factory, settings):
    await jobs.run_once(factory, FakeConnector(), settings, now=NOW)
    cost, usage = await _count(factory, CostLine.id), await _count(factory, TokenUsageRow.id)
    await jobs.run_once(factory, FakeConnector(), settings, now=NOW)
    assert await _count(factory, CostLine.id) == cost
    assert await _count(factory, TokenUsageRow.id) == usage


async def test_failing_account_does_not_block_others(factory, settings):
    connector = TwoAccountConnector()
    await jobs.run_inventory(factory, connector, settings)

    cost = await jobs.run_cost(factory, connector, settings, date(2026, 10, 12), date(2026, 10, 14))
    usage = await jobs.run_usage(factory, connector, settings, *jobs.usage_window(NOW))

    assert cost.failed == ["sub-bad"] and cost.rows > 0
    assert usage.failed == ["sub-bad"] and usage.rows > 0
    assert await _count(factory, CostLine.id, CostLine.sub_account_id == "sub-good") == cost.rows
    assert await _count(factory, CostLine.id, CostLine.sub_account_id == "sub-bad") == 0
    assert await _count(factory, TokenUsageRow.id, TokenUsageRow.account_id == "sub-good") > 0


async def test_usage_syncs_inventory_on_fresh_install(factory, settings):
    result = await jobs.run_usage(factory, FakeConnector(), settings, *jobs.usage_window(NOW))
    assert result.rows == 9
    assert await _count(factory, CloudAccountRow.id) == 1


async def test_backfill_covers_requested_days(factory, settings):
    await jobs.run_backfill(factory, FakeConnector(), settings, days=2, now=NOW)
    async with factory() as s:
        days = (await s.scalars(select(CostLine.charge_period_start).distinct())).all()
    assert sorted(days) == [date(2026, 10, 13), date(2026, 10, 14)]
    # Yesterday's 24 hours plus today up to and including the 15:00 bucket, x3 deployments.
    assert await _count(factory, TokenUsageRow.id) == (24 + 16) * 3


def test_windows():
    assert jobs.usage_window(NOW) == (
        datetime(2026, 10, 14, 13, tzinfo=UTC),
        datetime(2026, 10, 14, 16, tzinfo=UTC),
    )
    assert jobs.cost_window(date(2026, 10, 14)) == (date(2026, 10, 12), date(2026, 10, 14))


def test_worker_settings_schedule():
    from ai_cost_tracker.worker.settings import WorkerSettings

    crons = {c.name.removeprefix("cron:"): c for c in WorkerSettings.cron_jobs}
    assert crons["sync_inventory_job"].minute == 7
    assert crons["ingest_usage_job"].minute == set(range(0, 60, 5))
    assert crons["ingest_cost_job"].hour == {0, 4, 8, 12, 16, 20}
    assert crons["sync_prices_job"].hour == 2
    assert crons["discovery_job"].hour == 3
    assert crons["check_access_job"].minute == set(range(3, 60, 10))
    names = {getattr(f, "name", getattr(f, "__name__", None)) for f in WorkerSettings.functions}
    assert {"backfill_job", "backfill_account_job", "check_access_job"} <= names


async def test_inventory_failure_falls_back_to_stored_inventory(factory, settings):
    await jobs.run_inventory(factory, FakeConnector(), settings)

    class BrokenInventory(FakeConnector):
        async def list_accounts(self):
            raise RuntimeError("ARM throttled")

    result = await jobs.run_once(factory, BrokenInventory(), settings, now=NOW)
    assert result.failed == [] and result.rows > 0


# --- M1: tracked accounts, sync_state, watermarks, open invoice periods, jitter ---------


class ThreeAccountConnector(TwoAccountConnector):
    async def list_accounts(self) -> list[CloudAccount]:
        return [
            CloudAccount(cloud="azure", account_id=a, name=a, has_ai_resources=True)
            for a in ("sub-good", "sub-untracked", "sub-waiting")
        ]


async def _set_account(factory, account_id, **values):
    async with factory() as s:
        await s.execute(
            update(CloudAccountRow).where(CloudAccountRow.account_id == account_id).values(**values)
        )
        await s.commit()


async def _state(factory, account_id, source) -> SyncState | None:
    async with factory() as s:
        return await s.scalar(
            select(SyncState).where(SyncState.account_id == account_id, SyncState.source == source)
        )


async def test_scheduled_passes_skip_untracked_and_waiting_accounts(factory, settings):
    connector = ThreeAccountConnector()
    await jobs.run_inventory(factory, connector, settings)
    await _set_account(factory, "sub-untracked", tracked=False)
    await _set_account(factory, "sub-waiting", access_status="waiting_for_access")

    await jobs.run_usage(factory, connector, settings, now=NOW)
    await jobs.run_cost(factory, connector, settings, today=NOW.date())

    async with factory() as s:
        usage_accounts = set(await s.scalars(select(TokenUsageRow.account_id).distinct()))
        cost_accounts = set(await s.scalars(select(CostLine.sub_account_id).distinct()))
        state_accounts = set(
            await s.scalars(
                select(SyncState.account_id).where(SyncState.source.in_(["cost", "usage"]))
            )
        )
    assert usage_accounts == cost_accounts == state_accounts == {"sub-good"}


async def test_inventory_records_sync_state(factory, settings):
    await jobs.run_inventory(factory, TwoAccountConnector(), settings)
    state = await _state(factory, "sub-good", "inventory")
    assert state is not None and state.last_success_at is not None and state.last_error is None


async def test_usage_window_follows_watermark(factory, settings):
    connector = FakeConnector()
    await jobs.run_inventory(factory, connector, settings)
    sub = (await connector.list_accounts())[0].account_id

    # No watermark: two hours plus the current one.
    await jobs.run_usage(factory, connector, settings, now=NOW)
    state = await _state(factory, sub, "usage")
    assert jobs._utc(state.watermark) == datetime(2026, 10, 14, 15, tzinfo=UTC)
    assert await _count(factory, TokenUsageRow.id) == 9

    # Five hours later the window reaches back to one hour before the watermark.
    later = NOW + timedelta(hours=5)
    assert jobs.scheduled_usage_window(state.watermark, later) == (
        datetime(2026, 10, 14, 14, tzinfo=UTC),
        datetime(2026, 10, 14, 21, tzinfo=UTC),
    )
    await jobs.run_usage(factory, connector, settings, now=later)
    assert await _count(factory, TokenUsageRow.id) == (21 - 13) * 3
    state = await _state(factory, sub, "usage")
    assert jobs._utc(state.watermark) == datetime(2026, 10, 14, 20, tzinfo=UTC)


def test_scheduled_usage_window_bounds():
    assert jobs.scheduled_usage_window(None, NOW) == jobs.usage_window(NOW)
    old = NOW - timedelta(days=40)
    start, _ = jobs.scheduled_usage_window(old, NOW)
    assert start == datetime(2026, 10, 7, 15, tzinfo=UTC)
    # A watermark in the current hour still re-reads the previous hour.
    start, _ = jobs.scheduled_usage_window(NOW, NOW)
    assert start == datetime(2026, 10, 14, 14, tzinfo=UTC)


async def test_scheduled_cost_covers_previous_and_current_month(factory, settings):
    connector = FakeConnector()
    await jobs.run_cost(factory, connector, settings, today=date(2026, 10, 14))
    async with factory() as s:
        first = await s.scalar(select(func.min(CostLine.charge_period_start)))
        last = await s.scalar(select(func.max(CostLine.charge_period_start)))
    assert (first, last) == (date(2026, 9, 1), date(2026, 10, 14))
    assert jobs.open_invoice_window(date(2026, 1, 3)) == (date(2025, 12, 1), date(2026, 1, 3))
    sub = (await connector.list_accounts())[0].account_id
    state = await _state(factory, sub, "cost")
    assert jobs._utc(state.watermark) == datetime(2026, 10, 14, tzinfo=UTC)


async def test_per_account_failure_is_recorded(factory, settings):
    connector = TwoAccountConnector()
    await jobs.run_inventory(factory, connector, settings)

    cost = await jobs.run_cost(factory, connector, settings, today=NOW.date())
    usage = await jobs.run_usage(factory, connector, settings, now=NOW)

    assert cost.failed == usage.failed == ["sub-bad"]
    for source in ("cost", "usage"):
        bad = await _state(factory, "sub-bad", source)
        good = await _state(factory, "sub-good", source)
        assert bad.last_error == "AuthorizationFailed" and bad.last_success_at is None
        assert good.last_error is None and good.last_success_at is not None


async def test_cost_jitter_spreads_accounts_without_real_sleep(factory, settings):
    connector = TwoAccountConnector()
    await jobs.run_inventory(factory, connector, settings)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    await jobs.run_cost(
        factory, connector, settings, today=NOW.date(), jitter_seconds=600, sleep=fake_sleep
    )
    delays = sorted(jobs.jitter_delay(a, 600) for a in ("sub-bad", "sub-good"))
    assert sum(slept) == delays[-1] <= 600
    assert jobs.jitter_delay("sub-good", 600) == jobs.jitter_delay("sub-good", 600)
    assert jobs.jitter_delay("sub-good", 0) == 0


async def test_post_steps_tolerate_missing_modules(factory, settings, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_w5(name, *args, **kwargs):
        if name in ("ai_cost_tracker.estimate", "ai_cost_tracker.reconcile"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_w5)
    tenant = await jobs._tenant_id(factory, settings)
    assert await jobs.run_estimates(factory, tenant, NOW.date(), NOW.date()) is None
    assert await jobs.run_reconcile(factory, tenant, NOW.date(), NOW.date()) is None


async def test_access_check_starts_backfill_for_newly_connected(factory, settings, monkeypatch):
    import sys
    import types

    calls: list[str] = []
    module = types.ModuleType("ai_cost_tracker.worker.discovery")

    async def check_pending_access(session_factory, connector, settings):
        return ["sub-1", "sub-2"]

    module.check_pending_access = check_pending_access
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.worker.discovery", module)

    async def enqueue(account_id: str) -> None:
        calls.append(account_id)

    result = await jobs.run_access_check(factory, FakeConnector(), settings, enqueue=enqueue)
    assert result == calls == ["sub-1", "sub-2"]


async def test_m2_jobs_run_and_are_scheduled(engine, tenant_id, monkeypatch):
    """Every M2 job resolves to a real function, runs on an empty DB, and has a cron entry."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ai_cost_tracker.config import Settings
    from ai_cost_tracker.worker import jobs
    from ai_cost_tracker.worker.settings import M2_JOBS, WorkerSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(connector="fake", default_tenant_slug="internal", environment="test")
    for name in jobs._M2_JOBS:
        assert await jobs.run_m2(name, factory, settings) is not None, name
    scheduled = {c.coroutine for c in WorkerSettings.cron_jobs}
    assert all(job in scheduled for job in M2_JOBS)


async def test_m2_job_failure_is_logged_not_raised(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ai_cost_tracker.config import Settings
    from ai_cost_tracker.worker import jobs

    monkeypatch.setitem(jobs._M2_JOBS, "broken", ("ai_cost_tracker.does_not_exist", "run"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    assert await jobs.run_m2("broken", factory, Settings(environment="test")) is None
