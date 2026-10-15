import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from ai_cost_tracker import ingest
from ai_cost_tracker.connectors.fake import FakeConnector
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DeploymentRow,
    Tenant,
    TokenUsageRow,
    UnmappedMeter,
)
from ai_cost_tracker.ingest import (
    get_or_create_tenant,
    ingest_cost,
    ingest_usage,
    sync_inventory,
)

D1, D7 = date(2026, 3, 1), date(2026, 3, 7)
T0 = datetime(2026, 3, 1, tzinfo=UTC)


async def _cost_stats(session, tenant_id=None):
    stmt = select(func.count(), func.sum(CostLine.billed_cost))
    if tenant_id is not None:
        stmt = stmt.where(CostLine.tenant_id == tenant_id)
    count, total = (await session.execute(stmt)).one()
    return count, Decimal(str(total or 0))


class StubConnector:
    """Hand-made connector whose output each test sets directly."""

    name = "stub"

    def __init__(self, deployments=None, costs=None, usage=None):
        self.deployments = deployments or []
        self.costs = costs or []
        self.usage = usage or []

    async def list_accounts(self):
        return [CloudAccount(cloud="azure", account_id="sub-1", name="Sub one")]

    async def list_deployments(self, account_id):
        return list(self.deployments)

    async def pull_cost(self, account_id, start, end):
        return [c for c in self.costs if start <= c.charge_period_start <= end]

    async def pull_usage(self, account_id, deployments, start, end):
        return list(self.usage)


def _dep(name: str) -> Deployment:
    return Deployment(
        cloud="azure", account_id="sub-1", resource_id="/r/oai", name=name, model="gpt-4o"
    )


def _cost(day: date, cost: str, meter: str = "gpt-4o-0806 Inp glbl", resource="/r/oai"):
    return CostRecord(
        cloud="azure",
        sub_account_id="sub-1",
        resource_id=resource,
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
    )


async def test_get_or_create_tenant(session, tenant_id):
    assert await get_or_create_tenant(session, "internal") == tenant_id
    other = await get_or_create_tenant(session, "acme", "Acme")
    assert other != tenant_id
    assert await get_or_create_tenant(session, "acme") == other
    assert await session.scalar(select(func.count()).select_from(Tenant)) == 2


async def test_fake_cost_ingest_is_idempotent(session, tenant_id):
    fake = FakeConnector()
    [account] = await fake.list_accounts()
    n1 = await ingest_cost(session, tenant_id, fake, account.account_id, D1, D7)
    first = await _cost_stats(session)
    n2 = await ingest_cost(session, tenant_id, fake, account.account_id, D1, D7)
    assert n1 == n2 == 7 * 3 * 2
    assert await _cost_stats(session) == first
    assert first[0] == n1


async def test_cost_subrange_replaces_only_that_range(session, tenant_id):
    stub = StubConnector(costs=[_cost(D1 + timedelta(days=i), "1") for i in range(7)])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D7)
    # Billing corrections for days 3-4 only; the connector now reports different figures.
    stub.costs = [_cost(date(2026, 3, 3), "5"), _cost(date(2026, 3, 4), "7")]
    assert (
        await ingest_cost(session, tenant_id, stub, "sub-1", date(2026, 3, 3), date(2026, 3, 4))
        == 2
    )
    rows = dict(
        (await session.execute(select(CostLine.charge_period_start, CostLine.billed_cost))).all()
    )
    assert len(rows) == 7
    assert rows[date(2026, 3, 3)] == Decimal(5)
    assert rows[date(2026, 3, 4)] == Decimal(7)
    assert rows[D1] == rows[D7] == Decimal(1)


async def test_cost_rows_vanishing_from_range_are_removed(session, tenant_id):
    stub = StubConnector(costs=[_cost(D1, "1"), _cost(D1, "2", meter="other meter")])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    stub.costs = [_cost(D1, "1")]
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _cost_stats(session) == (1, Decimal(1))


async def test_cost_tenant_isolation(session, tenant_id):
    other = await get_or_create_tenant(session, "other")
    stub = StubConnector(costs=[_cost(D1, "3")])
    await ingest_cost(session, other, stub, "sub-1", D1, D1)
    stub.costs = [_cost(D1, "1")]
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _cost_stats(session, other) == (1, Decimal(3))
    assert await _cost_stats(session, tenant_id) == (1, Decimal(1))


async def test_cost_meter_parsing_and_unmapped(session, tenant_id, monkeypatch):
    azure_meters = pytest.importorskip("ai_cost_tracker.normalise.azure_meters")

    stub = StubConnector(costs=[_cost(D1, "1", resource=None)])
    monkeypatch.setattr(azure_meters, "parse_meter", lambda meter: None)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    row = await session.scalar(select(CostLine))
    assert row.ai_model is None and row.ai_direction is None
    assert row.resource_id == ""

    info = azure_meters.MeterInfo(
        model="gpt-4o", version="0806", direction="input", deployment_type="global"
    )
    monkeypatch.setattr(azure_meters, "parse_meter", lambda meter: info)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    session.expire_all()
    row = await session.scalar(select(CostLine))
    assert (row.ai_model, row.ai_direction) == ("gpt-4o", "input")


async def test_cost_works_without_meter_parser(session, tenant_id, monkeypatch):
    # A None entry in sys.modules makes the import raise ImportError.
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.normalise.azure_meters", None)
    stub = StubConnector(costs=[_cost(D1, "1")])
    assert await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1) == 1


async def test_duplicate_cost_records_are_summed(session, tenant_id):
    stub = StubConnector(costs=[_cost(D1, "1"), _cost(D1, "2")])
    assert await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1) == 1
    assert await _cost_stats(session) == (1, Decimal(3))


async def test_fake_usage_ingest_is_idempotent_and_ranged(session, tenant_id):
    fake = FakeConnector()
    found = await sync_inventory(session, tenant_id, fake)
    [(account_id, deps)] = found.items()
    end = T0 + timedelta(hours=6)
    n1 = await ingest_usage(session, tenant_id, fake, account_id, deps, T0, end)
    n2 = await ingest_usage(session, tenant_id, fake, account_id, deps, T0, end)
    assert n1 == n2 == 6 * 3
    count = select(func.count()).select_from(TokenUsageRow)
    assert await session.scalar(count) == n1

    # Re-ingest hours 2-3 with only one deployment: other hours keep all three deployments.
    n3 = await ingest_usage(
        session,
        tenant_id,
        fake,
        account_id,
        deps[:1],
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=4),
    )
    assert n3 == 2
    assert await session.scalar(count) == 4 * 3 + 2


async def test_usage_keeps_gateway_rows_and_other_tenants(session, tenant_id):
    other = await get_or_create_tenant(session, "other")
    dep = _dep("d1")

    def usage(source="metrics", tokens=10):
        return TokenUsage(
            cloud="azure",
            account_id="sub-1",
            resource_id=dep.resource_id,
            deployment=dep.name,
            bucket_start=T0,
            input_tokens=tokens,
            source=source,
        )

    stub = StubConnector(usage=[usage(tokens=99)])
    await ingest_usage(session, other, stub, "sub-1", [dep], T0, T0 + timedelta(hours=1))
    stub.usage = [usage(source="gateway", tokens=5)]
    await ingest_usage(session, tenant_id, stub, "sub-1", [dep], T0, T0 + timedelta(hours=1))
    stub.usage = [usage(tokens=10)]
    for _ in range(2):
        await ingest_usage(session, tenant_id, stub, "sub-1", [dep], T0, T0 + timedelta(hours=1))

    rows = (
        await session.execute(
            select(TokenUsageRow.tenant_id, TokenUsageRow.source, TokenUsageRow.input_tokens)
        )
    ).all()
    assert sorted(rows, key=str) == sorted(
        [(other, "metrics", 99), (tenant_id, "gateway", 5), (tenant_id, "metrics", 10)], key=str
    )


async def test_sync_inventory_marks_and_revives_deployments(session, tenant_id):
    stub = StubConnector(deployments=[_dep("a"), _dep("b")])
    found = await sync_inventory(session, tenant_id, stub)
    assert [d.name for d in found["sub-1"]] == ["a", "b"]
    account = await session.scalar(select(CloudAccountRow))
    assert account.name == "Sub one" and account.has_ai_resources
    assert account.last_synced_at is not None

    stub.deployments = [_dep("a")]
    await sync_inventory(session, tenant_id, stub)
    rows = {r.name: r for r in await session.scalars(select(DeploymentRow))}
    assert rows["a"].deleted_at is None
    assert rows["b"].deleted_at is not None

    stub.deployments = [_dep("a"), _dep("b").model_copy(update={"model": "gpt-4.1"})]
    await sync_inventory(session, tenant_id, stub)
    rows = {r.name: r for r in await session.scalars(select(DeploymentRow))}
    assert len(rows) == 2
    assert rows["b"].deleted_at is None
    assert rows["b"].model == "gpt-4.1"
    assert await session.scalar(select(func.count()).select_from(CloudAccountRow)) == 1


async def test_sync_inventory_tenant_isolation(session, tenant_id):
    other = await get_or_create_tenant(session, "other")
    stub = StubConnector(deployments=[_dep("a")])
    await sync_inventory(session, other, stub)
    stub.deployments = []
    await sync_inventory(session, tenant_id, stub)
    other_row = await session.scalar(select(DeploymentRow).where(DeploymentRow.tenant_id == other))
    assert other_row.deleted_at is None
    mine = await session.scalar(
        select(CloudAccountRow).where(CloudAccountRow.tenant_id == tenant_id)
    )
    assert mine.has_ai_resources is False


async def test_failed_ingest_leaves_previous_rows(session, tenant_id, monkeypatch):
    stub = StubConnector(costs=[_cost(D1, "1")])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)

    def explode(_tenant_id, _record):
        raise RuntimeError("boom")

    monkeypatch.setattr(ingest, "_cost_row", explode)
    with pytest.raises(RuntimeError):
        await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _cost_stats(session) == (1, Decimal(1))


async def test_sync_inventory_skips_unreadable_account(session, tenant_id, monkeypatch):
    stub = StubConnector(deployments=[_dep("a")])
    await sync_inventory(session, tenant_id, stub)

    async def boom(account_id):
        raise PermissionError("Reader removed")

    monkeypatch.setattr(stub, "list_deployments", boom)
    found = await sync_inventory(session, tenant_id, stub)
    assert found == {}
    row = await session.scalar(select(DeploymentRow))
    assert row.deleted_at is None  # stored inventory untouched, not marked deleted


UNKNOWN = "mystery-model-9000 Inp glbl"  # no known family: lands in the queue


async def _queue(session):
    session.expire_all()
    rows = await session.scalars(select(UnmappedMeter).order_by(UnmappedMeter.sku_meter))
    return [
        (r.sku_meter, r.first_seen, r.last_seen, Decimal(r.total_cost), r.currency) for r in rows
    ]


async def test_unmapped_meters_are_queued(session, tenant_id):
    stub = StubConnector(
        costs=[_cost(D1, "1.5", UNKNOWN), _cost(D7, "2.25", UNKNOWN), _cost(D1, "9")]
    )
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D7)
    assert await _queue(session) == [(UNKNOWN, D1, D7, Decimal("3.75"), "USD")]
    # The line is still stored, just without a model.
    line = await session.scalar(select(CostLine).where(CostLine.sku_meter == UNKNOWN).limit(1))
    assert line.ai_model is None


async def test_unmapped_queue_idempotent_and_spans_accounts(session, tenant_id):
    stub = StubConnector(costs=[_cost(D1, "1", UNKNOWN), _cost(D7, "2", UNKNOWN)])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D7)
    first = await _queue(session)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D7)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)  # sub-range re-run
    assert await _queue(session) == first == [(UNKNOWN, D1, D7, Decimal(3), "USD")]

    # The same meter in another account adds to the total; other tenants never see it.
    other_acct = _cost(D1 - timedelta(days=1), "4", UNKNOWN).model_copy(
        update={"sub_account_id": "sub-2"}
    )
    stub2 = StubConnector(costs=[other_acct])
    await ingest_cost(session, tenant_id, stub2, "sub-2", D1 - timedelta(days=5), D1)
    assert await _queue(session) == [(UNKNOWN, D1 - timedelta(days=1), D7, Decimal(7), "USD")]

    # A correction that removes the lines shrinks, then empties, the queue entry.
    stub.costs = []
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D7)
    assert await _queue(session) == [
        (UNKNOWN, D1 - timedelta(days=1), D1 - timedelta(days=1), Decimal(4), "USD")
    ]
    stub2.costs = []
    await ingest_cost(session, tenant_id, stub2, "sub-2", D1 - timedelta(days=5), D1)
    assert await _queue(session) == []


async def test_unmapped_queue_is_tenant_scoped(session, tenant_id):
    other = await get_or_create_tenant(session, "other")
    stub = StubConnector(costs=[_cost(D1, "1", UNKNOWN)])
    await ingest_cost(session, other, stub, "sub-1", D1, D1)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    rows = (await session.execute(select(UnmappedMeter.tenant_id, UnmappedMeter.total_cost))).all()
    assert sorted(t for t, _ in rows) == sorted([other, tenant_id])


async def test_unmapped_meter_cleared_once_it_parses(session, tenant_id, monkeypatch):
    from ai_cost_tracker.normalise import azure_meters

    stub = StubConnector(costs=[_cost(D1, "1", UNKNOWN)])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert len(await _queue(session)) == 1

    info = azure_meters.MeterInfo("mystery-model-9000", None, "input", "global")
    monkeypatch.setattr(azure_meters, "parse_meter", lambda meter: info)
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _queue(session) == []
    line = await session.scalar(select(CostLine))
    assert line.ai_model == "mystery-model-9000"


async def test_unmapped_queue_rolls_back_with_cost(session, tenant_id, monkeypatch):
    stub = StubConnector(costs=[_cost(D1, "1", UNKNOWN)])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)

    async def explode(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(ingest, "_upsert_unmapped", explode)
    stub.costs = [_cost(D1, "5", UNKNOWN)]
    with pytest.raises(RuntimeError):
        await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _cost_stats(session) == (1, Decimal(1))
    assert [r[3] for r in await _queue(session)] == [Decimal(1)]


async def test_no_queue_without_meter_parser(session, tenant_id, monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.normalise.azure_meters", None)
    stub = StubConnector(costs=[_cost(D1, "1", UNKNOWN)])
    await ingest_cost(session, tenant_id, stub, "sub-1", D1, D1)
    assert await _queue(session) == []


async def test_sync_inventory_persists_region(session, tenant_id):
    stub = StubConnector(deployments=[_dep("a").model_copy(update={"region": "swedencentral"})])
    await sync_inventory(session, tenant_id, stub)
    assert (await session.scalar(select(DeploymentRow))).region == "swedencentral"

    stub.deployments = [_dep("a").model_copy(update={"region": "eastus2"})]
    await sync_inventory(session, tenant_id, stub)
    session.expire_all()
    assert (await session.scalar(select(DeploymentRow))).region == "eastus2"


async def test_sync_inventory_new_account_keeps_defaults(session, tenant_id):
    await sync_inventory(session, tenant_id, StubConnector(deployments=[_dep("a")]))
    account = await session.scalar(select(CloudAccountRow))
    defaults = CloudAccountRow.__table__.c
    assert account.tracked is defaults.tracked.default.arg
    assert account.access_status == defaults.access_status.default.arg
    assert account.discovered_at is None


async def test_sync_inventory_leaves_tracking_alone(session, tenant_id):
    discovered = datetime(2026, 2, 1, tzinfo=UTC)
    session.add(
        CloudAccountRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id="sub-1",
            name="Old name",
            tracked=False,
            access_status="waiting_for_access",
            discovered_at=discovered,
        )
    )
    await session.commit()

    await sync_inventory(session, tenant_id, StubConnector(deployments=[_dep("a")]))
    session.expire_all()
    account = await session.scalar(select(CloudAccountRow))
    assert account.name == "Sub one" and account.has_ai_resources
    assert account.tracked is False
    assert account.access_status == "waiting_for_access"
    assert account.discovered_at.replace(tzinfo=UTC) == discovered
