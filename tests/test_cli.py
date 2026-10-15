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


@pytest.mark.parametrize("argv", [[], ["seed-demo", "--days", "0"], ["bogus"]])
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
    fn, connector, kwargs = calls[1]
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
    assert days == (2,)
    assert usage[0] > 0
