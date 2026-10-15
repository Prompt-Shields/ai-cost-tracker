"""Shared fixtures: an in-memory SQLite database with the full schema and one tenant."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_cost_tracker.db.models import Base, Tenant


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
async def tenant_id(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(slug="internal", name="Internal")
    session.add(tenant)
    await session.commit()
    return tenant.id
