"""Alembic migrations against a real Postgres (with or without TimescaleDB).

Skipped unless ACT_TEST_DATABASE_URL points at a disposable database, e.g.
``postgresql+asyncpg://postgres:pw@localhost:5432/postgres``. The test leaves it at base.
SQLite is deliberately not used: the migration targets Postgres features.
"""

import os

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_cost_tracker.db import migrate
from ai_cost_tracker.db.models import Base

URL = os.environ.get("ACT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="ACT_TEST_DATABASE_URL not set")

TABLES = {"tenant", "cloud_account", "ai_deployment", "cost_line", "token_usage", "price"}


def _diff(connection) -> list:
    ctx = MigrationContext.configure(connection)
    return compare_metadata(ctx, Base.metadata)


async def test_upgrade_downgrade_roundtrip() -> None:
    engine = create_async_engine(URL)
    try:
        migrate.downgrade(URL, "base")
        migrate.upgrade(URL)
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
            assert TABLES <= tables
            # Schema matches the models (the Timescale-only composite PK is not compared).
            assert await conn.run_sync(_diff) == []
            timescale = (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname='timescaledb'"))
            ).first()
            if timescale:
                hypertables = (
                    await conn.execute(
                        text("SELECT hypertable_name FROM timescaledb_information.hypertables")
                    )
                ).scalars()
                assert "token_usage" in set(hypertables)

        migrate.downgrade(URL, "base")
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
            assert not (TABLES & tables)

        # Upgrading again after a full downgrade must work (downgrade cleaned up fully).
        migrate.upgrade(URL)
        migrate.downgrade(URL, "base")
    finally:
        await engine.dispose()
