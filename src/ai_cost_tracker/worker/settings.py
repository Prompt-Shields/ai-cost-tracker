"""arq worker configuration. Run with ``arq ai_cost_tracker.worker.settings.WorkerSettings``."""

import logging
from typing import Any

from arq import cron, func
from arq.connections import RedisSettings

from ai_cost_tracker.config import get_settings
from ai_cost_tracker.db.session import get_engine
from ai_cost_tracker.worker.jobs import (
    backfill_account_job,
    backfill_job,
    budgets_job,
    check_access_job,
    daily_digest_job,
    discovery_job,
    ingest_cost_job,
    ingest_usage_job,
    notifications_job,
    ptu_job,
    spikes_daily_job,
    spikes_intraday_job,
    sync_inventory_job,
    sync_prices_job,
    untagged_weekly_job,
)

M2_JOBS = [
    budgets_job,
    notifications_job,
    daily_digest_job,
    spikes_daily_job,
    spikes_intraday_job,
    untagged_weekly_job,
    ptu_job,
]


async def on_startup(ctx: dict[str, Any]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Drop any pool created before the worker's event loop existed.
    await get_engine().dispose()


async def on_shutdown(ctx: dict[str, Any]) -> None:
    await get_engine().dispose()


class WorkerSettings:
    functions = [
        sync_inventory_job,
        ingest_usage_job,
        ingest_cost_job,
        backfill_job,
        # keep_result=0 frees the job id once it finishes, so re-tracking an account can
        # queue a new backfill straight away; 12 months under throttling can take hours.
        func(backfill_account_job, keep_result=0, timeout=4 * 3600),
        sync_prices_job,
        discovery_job,
        check_access_job,
        *M2_JOBS,
    ]
    cron_jobs = [
        # Off the hour so we don't pile onto ARM at :00 with everyone else.
        cron(sync_inventory_job, minute=7, run_at_startup=True),
        cron(ingest_usage_job, minute=set(range(0, 60, 5))),
        cron(ingest_cost_job, hour=set(range(0, 24, 4)), minute=23),
        cron(sync_prices_job, hour=2, minute=41, run_at_startup=True),
        cron(discovery_job, hour=3, minute=13),
        cron(check_access_job, minute=set(range(3, 60, 10))),
        # M2: alerts are raised by these jobs and delivered by the notifier.
        cron(budgets_job, minute=31),
        cron(ptu_job, minute=17),
        cron(spikes_intraday_job, minute={4, 19, 34, 49}),
        # After the 04:23 cost pass, so yesterday's billed figures are in.
        cron(spikes_daily_job, hour=6, minute=27),
        cron(untagged_weekly_job, hour=6, minute=37),  # acts on Mondays only
        cron(notifications_job, minute=set(range(2, 60, 5))),
        cron(daily_digest_job, hour=7, minute=53),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = on_startup
    on_shutdown = on_shutdown
    # The scheduled jobs finish far sooner; the cost pass includes up to 15 min of jitter.
    job_timeout = 3600
