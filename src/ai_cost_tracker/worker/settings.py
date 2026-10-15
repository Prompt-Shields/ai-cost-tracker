"""arq worker configuration. Run with ``arq ai_cost_tracker.worker.settings.WorkerSettings``."""

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from ai_cost_tracker.config import get_settings
from ai_cost_tracker.db.session import get_engine
from ai_cost_tracker.worker.jobs import (
    backfill_job,
    ingest_cost_job,
    ingest_usage_job,
    sync_inventory_job,
)


async def on_startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Drop any pool created before the worker's event loop existed.
    await get_engine().dispose()


async def on_shutdown(ctx: dict[str, Any]) -> None:
    await get_engine().dispose()


class WorkerSettings:
    functions = [sync_inventory_job, ingest_usage_job, ingest_cost_job, backfill_job]
    cron_jobs = [
        # Off the hour so we don't pile onto ARM at :00 with everyone else.
        cron(sync_inventory_job, minute=7, run_at_startup=True),
        cron(ingest_usage_job, minute=set(range(0, 60, 5))),
        cron(ingest_cost_job, hour=set(range(0, 24, 4)), minute=23),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    # Backfills of many subscriptions can take a while; the scheduled jobs finish far sooner.
    job_timeout = 3600
