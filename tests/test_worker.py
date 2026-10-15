from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage
from ai_cost_tracker.db.models import CloudAccountRow, CostLine, TokenUsageRow
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
    assert jobs.backfill_job in WorkerSettings.functions


async def test_inventory_failure_falls_back_to_stored_inventory(factory, settings):
    await jobs.run_inventory(factory, FakeConnector(), settings)

    class BrokenInventory(FakeConnector):
        async def list_accounts(self):
            raise RuntimeError("ARM throttled")

    result = await jobs.run_once(factory, BrokenInventory(), settings, now=NOW)
    assert result.failed == [] and result.rows > 0
