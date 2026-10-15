"""Daily discovery and access re-checks against SQLite with stub connectors."""

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.core.models import CloudAccount
from ai_cost_tracker.db.models import AlertEvent, CloudAccountRow, SyncState
from ai_cost_tracker.worker.discovery import check_pending_access, run_discovery

NEW = "aaaaaaaa-0000-0000-0000-000000000001"
NO_AI = "aaaaaaaa-0000-0000-0000-000000000002"
TRACKED = "aaaaaaaa-0000-0000-0000-000000000003"


class StubConnector:
    name = "stub"

    def __init__(self, accounts: list[CloudAccount] | None = None) -> None:
        self.accounts = accounts or []
        self.access: dict[str, bool | Exception] = {}
        self.include_all_calls: list[bool] = []

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        self.include_all_calls.append(include_all)
        return self.accounts

    async def check_access(self, account_id: str) -> bool:
        result = self.access.get(account_id, True)
        if isinstance(result, Exception):
            raise result
        return result


def _account(account_id: str, name: str, ai: bool = True) -> CloudAccount:
    return CloudAccount(cloud="azure", account_id=account_id, name=name, has_ai_resources=ai)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(default_tenant_slug="internal")


async def _accounts(factory) -> dict[str, CloudAccountRow]:
    async with factory() as s:
        return {r.account_id: r for r in (await s.scalars(select(CloudAccountRow))).all()}


async def _alerts(factory) -> dict[tuple[str, str], AlertEvent]:
    async with factory() as s:
        return {(a.kind, a.subject): a for a in (await s.scalars(select(AlertEvent))).all()}


async def _add(factory, tenant_id, account_id: str, *, tracked: bool, status: str) -> None:
    async with factory() as s:
        s.add(
            CloudAccountRow(
                tenant_id=tenant_id,
                cloud="azure",
                account_id=account_id,
                name=f"sub {account_id[-1]}",
                has_ai_resources=True,
                tracked=tracked,
                access_status=status,
            )
        )
        await s.commit()


# --- discovery -------------------------------------------------------------------------


async def test_new_subscription_inserted_untracked_with_alert(factory, settings, tenant_id):
    await _add(factory, tenant_id, TRACKED, tracked=True, status="connected")
    conn = StubConnector(
        [_account(NEW, "new-ai"), _account(NO_AI, "no-ai", ai=False), _account(TRACKED, "t")]
    )

    assert await run_discovery(factory, conn, settings) == [NEW]
    assert conn.include_all_calls == [True]

    rows = await _accounts(factory)
    assert set(rows) == {NEW, TRACKED}
    new = rows[NEW]
    assert (new.tracked, new.access_status, new.has_ai_resources) == (False, "connected", True)
    assert new.discovered_at is not None and new.tenant_id == tenant_id
    assert rows[TRACKED].tracked is True and rows[TRACKED].discovered_at is None

    alerts = await _alerts(factory)
    assert set(alerts) == {("new_subscription", NEW)}
    assert "new-ai" in alerts[("new_subscription", NEW)].message
    assert alerts[("new_subscription", NEW)].resolved_at is None


async def test_discovery_is_idempotent(factory, settings, tenant_id):
    conn = StubConnector([_account(NEW, "new-ai")])
    assert await run_discovery(factory, conn, settings) == [NEW]
    first = (await _accounts(factory))[NEW].discovered_at

    conn.accounts = [_account(NEW, "renamed")]
    assert await run_discovery(factory, conn, settings) == []
    rows = await _accounts(factory)
    assert len(rows) == 1
    assert rows[NEW].name == "renamed" and rows[NEW].tracked is False
    assert rows[NEW].discovered_at == first
    assert len(await _alerts(factory)) == 1


async def test_discovery_resolves_banner_once_tracked(factory, settings, tenant_id):
    conn = StubConnector([_account(NEW, "new-ai")])
    await run_discovery(factory, conn, settings)
    async with factory() as s:
        row = await s.scalar(select(CloudAccountRow).where(CloudAccountRow.account_id == NEW))
        row.tracked = True
        await s.commit()
    await run_discovery(factory, conn, settings)
    assert (await _alerts(factory))[("new_subscription", NEW)].resolved_at is not None


# --- access checks ---------------------------------------------------------------------


async def _access_state(factory, account_id: str) -> SyncState:
    async with factory() as s:
        return await s.scalar(
            select(SyncState).where(
                SyncState.account_id == account_id, SyncState.source == "access"
            )
        )


async def test_pending_becomes_connected(factory, settings, tenant_id):
    await _add(factory, tenant_id, NEW, tracked=True, status="waiting_for_access")
    conn = StubConnector()
    conn.access[NEW] = False

    assert await check_pending_access(factory, conn, settings) == []
    assert (await _accounts(factory))[NEW].access_status == "waiting_for_access"
    state = await _access_state(factory, NEW)
    assert state.last_success_at is None and "not granted" in state.last_error

    conn.access[NEW] = True
    assert await check_pending_access(factory, conn, settings) == [NEW]
    assert (await _accounts(factory))[NEW].access_status == "connected"
    state = await _access_state(factory, NEW)
    assert state.last_success_at is not None and state.last_error is None
    assert await _alerts(factory) == {}

    # Already connected: checked again but not reported as newly connected.
    assert await check_pending_access(factory, conn, settings) == []


async def test_connected_loses_access_then_regains(factory, settings, tenant_id):
    await _add(factory, tenant_id, TRACKED, tracked=True, status="connected")
    conn = StubConnector()
    conn.access[TRACKED] = False

    assert await check_pending_access(factory, conn, settings) == []
    assert (await _accounts(factory))[TRACKED].access_status == "no_access"
    alert = (await _alerts(factory))[("access_lost", TRACKED)]
    assert alert.resolved_at is None and "Reader" in alert.message

    # Still gone: one alert, still open.
    await check_pending_access(factory, conn, settings)
    assert len(await _alerts(factory)) == 1

    conn.access[TRACKED] = True
    assert await check_pending_access(factory, conn, settings) == [TRACKED]
    assert (await _accounts(factory))[TRACKED].access_status == "connected"
    assert (await _alerts(factory))[("access_lost", TRACKED)].resolved_at is not None

    # Lost again: the same alert reopens.
    conn.access[TRACKED] = False
    await check_pending_access(factory, conn, settings)
    alerts = await _alerts(factory)
    assert len(alerts) == 1 and alerts[("access_lost", TRACKED)].resolved_at is None


async def test_untracked_connected_accounts_not_checked(factory, settings, tenant_id):
    await _add(factory, tenant_id, NEW, tracked=False, status="connected")
    conn = StubConnector()
    conn.access[NEW] = False
    assert await check_pending_access(factory, conn, settings) == []
    assert (await _accounts(factory))[NEW].access_status == "connected"
    assert await _alerts(factory) == {}


async def test_transient_error_leaves_status_and_continues(factory, settings, tenant_id):
    await _add(factory, tenant_id, TRACKED, tracked=True, status="connected")
    await _add(factory, tenant_id, NEW, tracked=True, status="waiting_for_access")
    conn = StubConnector()
    request = httpx.Request("GET", "https://management.azure.com/")
    conn.access[NEW] = httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(500, request=request)
    )
    conn.access[TRACKED] = httpx.ConnectError("down")

    assert await check_pending_access(factory, conn, settings) == []
    rows = await _accounts(factory)
    assert rows[TRACKED].access_status == "connected"
    assert rows[NEW].access_status == "waiting_for_access"
    assert (await _access_state(factory, NEW)).last_error
    assert await _alerts(factory) == {}
