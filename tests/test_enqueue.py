from unittest.mock import AsyncMock

import pytest

from ai_cost_tracker.worker import enqueue


async def test_enqueue_backfill_uses_job_id(monkeypatch):
    pool = AsyncMock()
    monkeypatch.setattr(enqueue, "create_pool", AsyncMock(return_value=pool))

    await enqueue.enqueue_backfill("sub-1")

    pool.enqueue_job.assert_awaited_once_with(
        "backfill_account_job", "sub-1", "azure", _job_id="backfill:azure:sub-1"
    )
    pool.aclose.assert_awaited_once()


async def test_enqueue_backfill_swallows_connection_errors(monkeypatch, caplog):
    monkeypatch.setattr(
        enqueue, "create_pool", AsyncMock(side_effect=ConnectionError("redis down"))
    )
    await enqueue.enqueue_backfill("sub-1")
    assert "redis down" in caplog.text


@pytest.mark.parametrize("error", [ConnectionError("reset"), TimeoutError()])
async def test_enqueue_backfill_swallows_enqueue_errors(monkeypatch, error):
    pool = AsyncMock()
    pool.enqueue_job.side_effect = error
    monkeypatch.setattr(enqueue, "create_pool", AsyncMock(return_value=pool))
    await enqueue.enqueue_backfill("sub-1", cloud="azure")
    pool.aclose.assert_awaited_once()
