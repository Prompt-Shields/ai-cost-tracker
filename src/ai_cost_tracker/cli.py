"""``act`` command line: migrations, demo data, one-off ingestion runs and operator tools."""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

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

    spike = sub.add_parser(
        "spike-azure",
        help="check the connector's Azure assumptions (#2) with your az login; writes a report",
    )
    spike.add_argument("--subscription", required=True, help="subscription id to probe")
    spike.add_argument("--out", type=Path, default=Path("docs/spikes/azure-payg.md"))
    spike.add_argument("--days", type=_positive_int, default=3, help="days of cost to query (3)")
    spike.add_argument("--account", help="resource id of the AI account to probe (default: first)")
    spike.add_argument("--model", default="gpt-4o-mini", help="model for the Retail Prices check")

    cmp = sub.add_parser(
        "compare-cost-analysis",
        help="compare database cost with a Cost Analysis CSV export (#27); exit 1 if a day "
        "differs by more than the tolerance",
    )
    cmp.add_argument("--csv", type=Path, required=True, help="Cost Analysis CSV export")
    cmp.add_argument("--start", type=date.fromisoformat, help="first day (default: export's)")
    cmp.add_argument("--end", type=date.fromisoformat, help="last day (default: export's)")
    cmp.add_argument("--subscription", action="append", help="limit to subscription (repeatable)")
    cmp.add_argument("--tolerance", type=Decimal, default=Decimal("1"), help="percent per day (1)")
    cmp.add_argument(
        "--min-abs", type=Decimal, default=Decimal("0.01"), help="ignore diffs <= this"
    )
    cmp.add_argument("--cost-column", help="export column to use (default: Cost, PreTaxCost, ...)")
    cmp.add_argument("--dayfirst", action="store_true", default=None, help="dates are DD/MM/YYYY")
    cmp.add_argument("--all-resources", action="store_true", help="don't skip non-AI resources")
    cmp.add_argument("--json", action="store_true", help="print JSON instead of a table")
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
    connector = FakeConnector()
    # Prices first: the backfill computes estimates and reconciles against them.
    asyncio.run(_with_db(seed_prices, connector))
    return asyncio.run(_with_db(run_backfill, connector, days=days))


async def seed_prices(session_factory, connector: Connector, settings) -> PassResult:
    from ai_cost_tracker.pricing.demo import seed_demo_prices

    async with session_factory() as session:
        await seed_demo_prices(session)
    return PassResult()


def ingest_once() -> PassResult:
    return asyncio.run(_with_db(run_once, get_connector(get_settings())))


def spike_azure(args: argparse.Namespace) -> int:
    """Run the live checks and write the sanitised report; checks never abort the run."""
    from ai_cost_tracker.tools import spike

    report, san = asyncio.run(
        spike.run_spike(args.subscription, args.days, account=args.account, model=args.model)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(spike.render_report(report, san), encoding="utf-8")
    for line in spike.summary_lines(report, san):
        print(line)
    counts = report.counts()
    print(
        f"\n{counts['confirmed']} confirmed, {counts['different']} different, "
        f"{counts['unchecked']} couldn't check. Report: {args.out}"
    )
    if report.auth_error:
        print("No Azure credential: run `az login` (and `az account set`) first.", file=sys.stderr)
        return 1
    return 0


async def _compare(args: argparse.Namespace):
    from ai_cost_tracker.db.session import get_engine, get_sessionmaker
    from ai_cost_tracker.tools import compare

    export = compare.read_export(
        args.csv,
        cost_column=args.cost_column,
        dayfirst=args.dayfirst,
        subscription_ids=args.subscription,
        all_resources=args.all_resources,
    )
    try:
        async with get_sessionmaker()() as session:
            return await compare.run_comparison(
                session,
                get_settings().default_tenant_slug,
                export,
                args.start,
                args.end,
                args.subscription,
                args.tolerance,
                args.min_abs,
            )
    finally:
        await get_engine().dispose()


def compare_cost_analysis(args: argparse.Namespace) -> int:
    """0 when every day is within tolerance, 1 when one isn't, 2 when the input is unusable."""
    from ai_cost_tracker.tools import compare

    try:
        result = asyncio.run(_compare(args))
    except (OSError, ValueError) as exc:
        print(f"compare-cost-analysis: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(compare.to_json(result), indent=2))
    else:
        print(compare.render_table(result))
    return 0 if result.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "migrate":
        migrate()
        return 0
    if args.command == "spike-azure":
        return spike_azure(args)
    if args.command == "compare-cost-analysis":
        return compare_cost_analysis(args)
    result = seed_demo(args.days) if args.command == "seed-demo" else ingest_once()
    print(f"{args.command}: {result.rows} rows written")
    if result.failed:
        print(f"failed accounts: {', '.join(result.failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
