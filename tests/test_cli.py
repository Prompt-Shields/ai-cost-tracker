import sqlite3

import pytest
from sqlalchemy import create_engine

from ai_cost_tracker import cli
from ai_cost_tracker.config import get_settings
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.db import session as db_session
from ai_cost_tracker.db.models import Base
from ai_cost_tracker.worker.jobs import PassResult, run_backfill, run_once


def test_parser_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["migrate"]).command == "migrate"
    assert parser.parse_args(["seed-demo"]).days == 30
    assert parser.parse_args(["seed-demo", "--days", "7"]).days == 7
    assert parser.parse_args(["ingest-once"]).command == "ingest-once"


def test_parser_operator_tools():
    parser = cli.build_parser()
    args = parser.parse_args(["spike-azure", "--subscription", "sub-1"])
    assert (args.subscription, args.days, str(args.out)) == (
        "sub-1",
        3,
        "docs/spikes/azure-payg.md",
    )
    args = parser.parse_args(
        ["compare-cost-analysis", "--csv", "x.csv", "--start", "2026-10-01", "--json"]
    )
    assert str(args.start) == "2026-10-01" and args.end is None and args.json
    assert str(args.tolerance) == "1"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["seed-demo", "--days", "0"],
        ["bogus"],
        ["compare-cost-analysis"],
        ["compare-cost-analysis", "--csv", "x.csv", "--start", "12/10/2026"],
    ],
)
def test_parser_rejects_bad_input(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


@pytest.fixture
def calls(monkeypatch):
    """Record migrate and the ingestion pass instead of touching a database."""
    log: list = []
    monkeypatch.setattr(cli, "migrate", lambda: log.append(("migrate",)))

    async def fake_with_db(fn, connector, **kwargs):
        log.append((fn, connector, kwargs))
        return PassResult(rows=5)

    monkeypatch.setattr(cli, "_with_db", fake_with_db)
    return log


@pytest.fixture
def azure_settings(monkeypatch):
    monkeypatch.setenv("ACT_CONNECTOR", "azure")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_seed_demo_migrates_then_ingests_with_fake_connector(calls, azure_settings):
    assert cli.main(["seed-demo", "--days", "3"]) == 0
    assert calls[0] == ("migrate",)
    assert calls[1][0] is cli.seed_prices
    fn, connector, kwargs = calls[2]
    assert fn is run_backfill
    assert isinstance(connector, FakeConnector)
    assert kwargs == {"days": 3}


def test_ingest_once_uses_configured_connector(calls, monkeypatch):
    monkeypatch.setattr(cli, "get_connector", lambda settings: "configured")
    assert cli.main(["ingest-once"]) == 0
    assert calls == [(run_once, "configured", {})]


def test_migrate_command(calls):
    assert cli.main(["migrate"]) == 0
    assert calls == [("migrate",)]


def test_seed_demo_end_to_end_on_sqlite(tmp_path, monkeypatch, azure_settings):
    """Real ingest against a SQLite file; only the Alembic migration is replaced by create_all."""
    path = tmp_path / "act.db"
    monkeypatch.setenv("ACT_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()

    def fake_migrate():
        Base.metadata.create_all(create_engine(f"sqlite:///{path}"))

    monkeypatch.setattr(cli, "migrate", fake_migrate)
    try:
        assert cli.main(["seed-demo", "--days", "2"]) == 0
    finally:
        db_session.get_engine.cache_clear()
        db_session.get_sessionmaker.cache_clear()

    with sqlite3.connect(path) as conn:
        days = conn.execute("select count(distinct charge_period_start) from cost_line").fetchone()
        usage = conn.execute("select count(*) from token_usage").fetchone()
    assert days == (1,)  # yesterday only: like real billing, today isn't billed yet
    assert usage[0] > 0
