from datetime import date

from sqlalchemy import select

from ai_cost_tracker import sync_state
from ai_cost_tracker.db.models import SyncState


async def test_record_success_then_error_keeps_last_success(session, tenant_id):
    await sync_state.record(session, tenant_id, "azure", "sub-1", "cost")
    row = await session.scalar(select(SyncState))
    first_success = row.last_success_at
    await sync_state.record(session, tenant_id, "azure", "sub-1", "cost", error="403 Forbidden")
    row = await session.scalar(select(SyncState))
    assert row.last_error == "403 Forbidden"
    assert row.last_success_at == first_success


async def test_record_backfill_clamps_progress(session, tenant_id):
    await sync_state.record_backfill(
        session,
        tenant_id,
        "azure",
        "sub-1",
        "cost",
        backfill_from=date(2025, 10, 1),
        done_through=None,
        progress=1.7,
    )
    row = await session.scalar(select(SyncState))
    assert row.backfill_progress == 1
