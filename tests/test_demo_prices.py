from datetime import date, timedelta

from sqlalchemy import func, select

from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.db.models import AlertEvent, DailyEstimate, Price
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.ingest import ingest_cost, ingest_usage, sync_inventory
from ai_cost_tracker.pricing.demo import seed_demo_prices
from ai_cost_tracker.reconcile import reconcile


async def test_demo_prices_make_demo_estimates_match_billed(session, tenant_id):
    assert await seed_demo_prices(session) == 6
    await seed_demo_prices(session)  # idempotent
    assert await session.scalar(select(func.count()).select_from(Price)) == 6

    fake = FakeConnector()
    found = await sync_inventory(session, tenant_id, fake)
    ((account_id, deployments),) = found.items()
    day = date(2026, 10, 7)
    start = datetime_at(day)
    await ingest_usage(
        session, tenant_id, fake, account_id, deployments, start, start + timedelta(days=1)
    )
    await ingest_cost(session, tenant_id, fake, account_id, day, day)
    await compute_estimates(session, tenant_id, day, day)
    await reconcile(session, tenant_id, day, day, today=day + timedelta(days=2))

    rows = (await session.scalars(select(DailyEstimate))).all()
    assert rows and not any(r.price_missing for r in rows)
    assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 0


async def test_no_variance_alert_when_price_missing(session, tenant_id):
    fake = FakeConnector()
    found = await sync_inventory(session, tenant_id, fake)
    ((account_id, deployments),) = found.items()
    day = date(2026, 10, 7)
    start = datetime_at(day)
    await ingest_usage(
        session, tenant_id, fake, account_id, deployments, start, start + timedelta(days=1)
    )
    await ingest_cost(session, tenant_id, fake, account_id, day, day)
    await compute_estimates(session, tenant_id, day, day)
    await reconcile(session, tenant_id, day, day, today=day + timedelta(days=2))
    assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 0


def datetime_at(day):
    from datetime import UTC, datetime

    return datetime(day.year, day.month, day.day, tzinfo=UTC)
