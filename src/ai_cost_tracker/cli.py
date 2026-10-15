"""``act`` command line: migrations, demo data and one-off ingestion runs."""

import argparse
import asyncio
import logging
from collections.abc import Sequence

from ai_cost_tracker.config import get_settings
from ai_cost_tracker.connectors import Connector, get_connector
from ai_cost_tracker.worker.jobs import PassResult, run_backfill, run_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="act", description="AI cost tracker admin commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    seed = sub.add_parser("seed-demo", help="migrate and load demo data from the fake connector")
    seed.add_argument("--days", type=_positive_int, default=30, help="days of history (30)")
    sub.add_parser("ingest-once", help="run one inventory + cost + usage pass and exit")
    return parser


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def migrate() -> None:
    from ai_cost_tracker.db.migrate import upgrade

    # Alembic's async env.py runs its own event loop, so call it outside asyncio.run.
    upgrade()


async def _with_db(coro_fn, connector: Connector, **kwargs) -> PassResult:
    from ai_cost_tracker.db.session import get_engine, get_sessionmaker

    try:
        return await coro_fn(get_sessionmaker(), connector, get_settings(), **kwargs)
    finally:
        await get_engine().dispose()


def seed_demo(days: int) -> PassResult:
    """Demo data always comes from the FakeConnector, whatever ``ACT_CONNECTOR`` says."""
    from ai_cost_tracker.connectors.fake import FakeConnector

    migrate()
    return asyncio.run(_with_db(run_backfill, FakeConnector(), days=days))


def ingest_once() -> PassResult:
    return asyncio.run(_with_db(run_once, get_connector(get_settings())))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "migrate":
        migrate()
        return 0
    result = seed_demo(args.days) if args.command == "seed-demo" else ingest_once()
    print(f"{args.command}: {result.rows} rows written")
    if result.failed:
        print(f"failed accounts: {', '.join(result.failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
