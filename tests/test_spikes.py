"""Spike detection (issue #31): one alert per injected spike, never a flood."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker import ingest, spikes
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.db.models import (
    AlertEvent,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    TokenUsageRow,
)

NOW = datetime(2026, 10, 16, 9, 7, tzinfo=UTC)  # a Friday
YESTERDAY = date(2026, 10, 15)
RG = "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts"
RES = f"{RG}/oai-main"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _deployment(session, tenant_id, name: str, model: str = "gpt-4o") -> None:
    session.add(
        DeploymentRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id="sub-1",
            resource_id=RES,
            name=name,
            model=model,
        )
    )


async def _history(session, tenant_id, name: str, daily: str, days: int = 35, **overrides):
    """``days`` of estimates ending yesterday at ``daily`` $/day; ``overrides`` by ISO day."""
    await _deployment(session, tenant_id, name)
    for i in range(days):
        day = YESTERDAY - timedelta(days=i)
        cost = Decimal(overrides.get(day.isoformat(), daily))
        session.add(
            DailyEstimate(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                resource_id=RES,
                deployment=name,
                model="gpt-4o",
                day=day,
                estimated_cost=cost,
            )
        )
    await session.commit()


async def _alerts(session, kind: str = spikes.KIND_DAILY) -> list[AlertEvent]:
    session.expire_all()
    return list(await session.scalars(select(AlertEvent).where(AlertEvent.kind == kind)))


async def _set_estimate(session, name: str, day: date, cost: str) -> None:
    await session.execute(
        update(DailyEstimate)
        .where(DailyEstimate.deployment == name, DailyEstimate.day == day)
        .values(estimated_cost=Decimal(cost))
    )
    await session.commit()


# --- daily -----------------------------------------------------------------------------


async def test_injected_spike_raises_exactly_one_alert(factory, session, tenant_id):
    await _history(session, tenant_id, "chat", "20", **{YESTERDAY.isoformat(): "60"})
    await _history(session, tenant_id, "steady", "20")

    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 1
    [alert] = await _alerts(session)
    assert alert.subject == f"{RES}/chat/2026-10-15"
    assert alert.severity == "warning"
    assert alert.resolved_at is None and alert.notified_at is None
    assert alert.details["cost"] == "60.0000000000"
    assert Decimal(alert.details["baseline"]) == 20
    assert alert.details["ratio"] == "3.00"
    assert "oai-main/chat" in alert.message

    # Re-running (the job runs again, billing restates) never duplicates or re-notifies.
    alert.notified_at = NOW
    await session.commit()
    await spikes.run_spike_detection(factory, _settings(), now=NOW + timedelta(hours=6))
    [again] = await _alerts(session)
    assert again.id == alert.id and again.notified_at is not None


async def test_escalation_to_critical_renotifies(factory, session, tenant_id):
    await _history(session, tenant_id, "chat", "20", **{YESTERDAY.isoformat(): "60"})
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    alert.notified_at = NOW
    await session.commit()

    await _set_estimate(session, "chat", YESTERDAY, "150")
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    assert alert.severity == "critical"
    assert alert.notified_at is None
    assert alert.details["ratio"] == "7.50"


async def test_spike_that_goes_away_is_resolved(factory, session, tenant_id):
    await _history(session, tenant_id, "chat", "20", **{YESTERDAY.isoformat(): "60"})
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    await _set_estimate(session, "chat", YESTERDAY, "22")
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0
    [alert] = await _alerts(session)
    assert alert.resolved_at is not None


async def test_manually_resolved_spike_stays_resolved(factory, session, tenant_id):
    await _history(session, tenant_id, "chat", "20", **{YESTERDAY.isoformat(): "60"})
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    alert.resolved_at = NOW
    alert.details = {**alert.details, "resolved_by": "a1"}
    await session.commit()

    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    assert alert.resolved_at is not None


async def test_tiny_deployment_below_floor_is_ignored(factory, session, tenant_id):
    # 8x its usual spend, but only $3.50 more: not worth a page.
    await _history(session, tenant_id, "tiny", "0.50", **{YESTERDAY.isoformat(): "4"})
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0
    assert await _alerts(session) == []


async def test_insufficient_history_raises_nothing(factory, session, tenant_id):
    await _history(session, tenant_id, "new", "20", days=6, **{YESTERDAY.isoformat(): "500"})
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0
    assert await _alerts(session) == []


async def test_weekly_pattern_is_not_a_spike(factory, session, tenant_id):
    # Thursdays are batch-job days: 5x the rest of the week, every week.
    thursdays = {(YESTERDAY - timedelta(weeks=w)).isoformat(): "100" for w in range(5)}
    await _history(session, tenant_id, "batch", "20", **thursdays)
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0


async def test_zero_baseline_is_critical(factory, session, tenant_id):
    await _history(session, tenant_id, "idle", "0", **{YESTERDAY.isoformat(): "40"})
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    assert alert.severity == "critical" and alert.details["ratio"] is None


async def test_billed_cost_wins_over_estimate(factory, session, tenant_id):
    await _history(session, tenant_id, "chat", "20")  # estimate says a normal day
    session.add(
        CostLine(
            tenant_id=tenant_id,
            cloud="azure",
            sub_account_id="sub-1",
            resource_id=RES,
            charge_period_start=YESTERDAY,
            sku_meter="gpt-4o-0806 Inp glbl",
            billed_cost=Decimal("90"),
            billing_currency="USD",
            ai_model="gpt-4o",
            ai_direction="input",
        )
    )
    await session.commit()
    await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    assert alert.details["cost"] == "90.0000000000"


async def test_no_tenant_is_a_no_op(factory):
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0
    assert await spikes.run_intraday_spikes(factory, _settings(), now=NOW) == 0


async def test_fake_connector_data_with_injected_spike(factory, session, tenant_id):
    """End to end on the FakeConnector's billing: quiet normally, one alert when spiked."""
    connector = FakeConnector(today=NOW.date())
    found = await ingest.sync_inventory(session, tenant_id, connector)
    [account] = found
    await ingest.ingest_cost(
        session, tenant_id, connector, account, YESTERDAY - timedelta(days=35), YESTERDAY
    )
    assert await spikes.run_spike_detection(factory, _settings(), now=NOW) == 0

    await session.execute(
        update(CostLine)
        .where(CostLine.resource_id.like("%/oai-search"), CostLine.charge_period_start == YESTERDAY)
        .values(billed_cost=CostLine.billed_cost * 4)
    )
    await session.commit()
    for _ in range(2):
        await spikes.run_spike_detection(factory, _settings(), now=NOW)
    [alert] = await _alerts(session)
    assert alert.subject.endswith("/oai-search/search-rag-gpt-4o/2026-10-15")


# --- intraday --------------------------------------------------------------------------


HOUR = NOW.replace(minute=0)  # 09:00; window is 07:00-09:00


async def _hourly(session, tenant_id, name: str, per_hour: int, days: int = 29) -> None:
    start = HOUR - timedelta(days=days)
    hour = start
    while hour < HOUR + timedelta(hours=3):
        session.add(
            TokenUsageRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id="sub-1",
                resource_id=RES,
                deployment=name,
                model="gpt-4o",
                bucket_start=hour,
                input_tokens=per_hour,
                output_tokens=0,
            )
        )
        hour += timedelta(hours=1)
    await session.commit()


async def _set_hours(session, name: str, per_hour: int, end: datetime = HOUR) -> None:
    await session.execute(
        update(TokenUsageRow)
        .where(
            TokenUsageRow.deployment == name,
            TokenUsageRow.bucket_start >= end - timedelta(hours=2),
            TokenUsageRow.bucket_start < end,
        )
        .values(input_tokens=per_hour)
    )
    await session.commit()


async def test_intraday_spike_and_escalation(factory, session, tenant_id):
    await _hourly(session, tenant_id, "chat", 100_000)
    await _hourly(session, tenant_id, "steady", 100_000)
    await _set_hours(session, "chat", 300_000)

    assert await spikes.run_intraday_spikes(factory, _settings(), now=NOW) == 1
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    assert alert.subject == f"{RES}/chat/2026-10-16"
    assert alert.details["ratio"] == "3.00" and alert.severity == "warning"
    alert.notified_at = NOW
    await session.commit()

    # 15 minutes later, a bit worse but not 50% worse: updated, not re-sent.
    await _set_hours(session, "chat", 400_000)
    await spikes.run_intraday_spikes(factory, _settings(), now=NOW + timedelta(minutes=15))
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    assert alert.details["ratio"] == "4.00"
    assert alert.notified_at is not None
    assert alert.details["notified_ratio"] == "3.00"

    # 50% above the notified ratio: re-sent, now critical.
    await _set_hours(session, "chat", 600_000)
    await spikes.run_intraday_spikes(factory, _settings(), now=NOW + timedelta(minutes=30))
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    assert alert.notified_at is None
    assert alert.details["notified_ratio"] == "6.00"
    assert alert.details["peak_ratio"] == "6.00"
    assert alert.severity == "critical"

    # Next hour back to normal: resolved, still one row.
    await spikes.run_intraday_spikes(factory, _settings(), now=NOW + timedelta(hours=2))
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    assert alert.resolved_at is not None


async def test_intraday_respike_same_day_does_not_renotify(factory, session, tenant_id):
    await _hourly(session, tenant_id, "chat", 100_000)
    await _set_hours(session, "chat", 300_000)
    await spikes.run_intraday_spikes(factory, _settings(), now=NOW)
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    alert.notified_at = NOW
    await session.commit()

    later = HOUR + timedelta(hours=2)
    await spikes.run_intraday_spikes(factory, _settings(), now=later - timedelta(hours=1))
    await _set_hours(session, "chat", 300_000, end=later)
    await spikes.run_intraday_spikes(factory, _settings(), now=later)
    [alert] = await _alerts(session, spikes.KIND_INTRADAY)
    assert alert.resolved_at is None
    assert alert.notified_at is not None


async def test_intraday_floor_and_history(factory, session, tenant_id):
    await _hourly(session, tenant_id, "small", 1_000)  # 10x, but only 20k tokens
    await _set_hours(session, "small", 10_000)
    await _hourly(session, tenant_id, "new", 100_000, days=5)  # spiking, but too new
    await _set_hours(session, "new", 1_000_000)
    assert await spikes.run_intraday_spikes(factory, _settings(), now=NOW) == 0
    assert await _alerts(session, spikes.KIND_INTRADAY) == []


async def test_intraday_fake_connector_with_injected_spike(factory, session, tenant_id):
    connector = FakeConnector(today=NOW.date())
    found = await ingest.sync_inventory(session, tenant_id, connector)
    [(account, deployments)] = found.items()
    await ingest.ingest_usage(
        session, tenant_id, connector, account, deployments, HOUR - timedelta(days=29), HOUR
    )
    assert await spikes.run_intraday_spikes(factory, _settings(), now=NOW) == 0

    await session.execute(
        update(TokenUsageRow)
        .where(
            TokenUsageRow.deployment == "support-bot-gpt-4o-mini",
            TokenUsageRow.bucket_start >= HOUR - timedelta(hours=2),
        )
        .values(input_tokens=TokenUsageRow.input_tokens * 5)
    )
    await session.commit()
    for minutes in (0, 15, 30):
        await spikes.run_intraday_spikes(factory, _settings(), now=NOW + timedelta(minutes=minutes))
    count = await session.scalar(select(func.count()).select_from(AlertEvent))
    assert count == 1
