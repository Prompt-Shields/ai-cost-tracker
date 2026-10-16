"""Hand work to the arq worker from the API (e.g. a backfill when an account connects).

Enqueueing is best effort: if Redis is down the caller's request still succeeds, and the
scheduled passes plus the access check pick the account up later.
"""

import logging

from arq import create_pool
from arq.connections import RedisSettings

from ai_cost_tracker.config import get_settings

log = logging.getLogger(__name__)


def backfill_job_id(cloud: str, account_id: str) -> str:
    """One queued backfill per account: arq drops a second job with the same id."""
    return f"backfill:{cloud}:{account_id}"


async def enqueue_backfill(account_id: str, cloud: str = "azure") -> None:
    """Queue ``backfill_account_job`` for the account; log and swallow Redis errors."""
    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    except Exception as exc:
        log.warning("could not reach Redis to enqueue backfill for %s: %s", account_id, exc)
        return
    try:
        job = await pool.enqueue_job(
            "backfill_account_job",
            account_id,
            cloud,
            _job_id=backfill_job_id(cloud, account_id),
        )
        if job is None:
            log.info("backfill for %s/%s already queued", cloud, account_id)
    except Exception as exc:
        log.warning("could not enqueue backfill for %s: %s", account_id, exc)
    finally:
        try:
            await pool.aclose()
        except Exception:
            log.debug("closing the Redis pool failed", exc_info=True)
