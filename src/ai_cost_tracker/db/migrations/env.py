"""Alembic environment: async (asyncpg), autogenerate against ``Base.metadata``."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from ai_cost_tracker.config import get_settings
from ai_cost_tracker.db.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    # Keep the caller's loggers alive when run programmatically (e.g. from the worker).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """``-x url=...`` beats ``sqlalchemy.url`` beats ACT_DATABASE_URL."""
    x_url = context.get_x_argument(as_dictionary=True).get("url")
    return x_url or config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
