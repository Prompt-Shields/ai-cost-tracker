"""PTU utilisation ingest and idle-capacity report maths (issue #32)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker import ptu
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.base import is_provisioned
from ai_cost_tracker.connectors.fake import PTU_METER, FakeConnector
from ai_cost_tracker.db.models import CloudAccountRow, CostLine, DeploymentRow, PtuUtilization
from ai_cost_tracker.ingest import sync_inventory

NOW = datetime(2026, 10, 14, 15, 20, tzinfo=UTC)  # a Wednesday afternoon
RES = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-ptu"
OTHER = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/oai-x"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


async def _rows(session) -> list[tuple]:
    rows = await session.execute(
        select(
            PtuUtilization.resource_id,
            PtuUtilization.deployment,
            PtuUtilization.bucket_start,
            PtuUtilization.utilization,
        ).order_by(PtuUtilization.deployment, PtuUtilization.bucket_start)
    )
    return [tuple(r) for r in rows]


# --- fake connector --------------------------------------------------------------------


async def test_fake_has_one_ptu_deployment_with_daily_curve():
    fake = FakeConnector()
    (account,) = await fake.list_accounts()
    deps = await fake.list_deployments(account.account_id)
    ptus = [d for d in deps if is_provisioned(d.sku)]
    assert [(d.name, d.capacity, d.sku) for d in ptus] == [
        ("batch-ptu-gpt-4o", 50, "ProvisionedManaged")
    ]
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    samples = await fake.pull_ptu_utilization(
        account.account_id, deps, monday, monday + timedelta(days=7)
    )
    assert len(samples) == 7 * 24
    assert {s.deployment for s in samples} == {"batch-ptu-gpt-4o"}
    by_hour = {s.bucket_start: s.utilization for s in samples}
    assert by_hour[monday.replace(hour=11)] > Decimal("0.7")  # weekday daytime: busy
    assert by_hour[monday.replace(hour=2)] < Decimal("0.1")  # night: idle
    saturday_noon = monday + timedelta(days=5, hours=12)
    assert by_hour[saturday_noon] < Decimal("0.15")  # weekend: mostly idle
    assert all(s.max_utilization >= s.utilization for s in samples)
    # Deterministic, so a re-pull replaces with identical rows.
    again = await fake.pull_ptu_utilization(
        account.account_id, deps, monday, monday + timedelta(days=7)
    )
    assert again == samples


async def test_fake_bills_ptu_hours_on_model_agnostic_meter():
    fake = FakeConnector()
    (account,) = await fake.list_accounts()
    day = date(2026, 10, 12)
    ptu_lines = [
        r for r in await fake.pull_cost(account.account_id, day, day) if r.sku_meter == PTU_METER
    ]
    assert len(ptu_lines) == 1
    assert ptu_lines[0].consumed_quantity == 50 * 24
    assert ptu_lines[0].billed_cost == Decimal("1200.00")


# --- ingest ----------------------------------------------------------------------------


async def test_run_ptu_is_idempotent_and_only_tracked_connected(engine, session, tenant_id):
    fake = FakeConnector()
    await sync_inventory(session, tenant_id, fake)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    n = await ptu.run_ptu(factory, _settings(), now=NOW, connector=fake)
    assert n == 3  # 13:00, 14:00 and the current 15:00 hour
    first = await _rows(session)
    assert [r[2].hour for r in first] == [13, 14, 15]
    assert {r[1] for r in first} == {"batch-ptu-gpt-4o"}

    assert await ptu.run_ptu(factory, _settings(), now=NOW, connector=fake) == 3
    assert await _rows(session) == first

    account = await session.scalar(select(CloudAccountRow))
    account.tracked = False
    await session.commit()
    assert await ptu.run_ptu(factory, _settings(), now=NOW, connector=fake) == 0
    account.tracked, account.access_status = True, "no_access"
    await session.commit()
    assert await ptu.run_ptu(factory, _settings(), now=NOW, connector=fake) == 0


async def test_ingest_replaces_window_when_connector_changes_its_answer(session, tenant_id):
    fake = FakeConnector()
    found = await sync_inventory(session, tenant_id, fake)
    ((account_id, deployments),) = found.items()
    start, end = NOW.replace(hour=10, minute=0), NOW.replace(hour=13, minute=0)
    assert await ptu.ingest_ptu(session, tenant_id, fake, account_id, deployments, start, end) == 3

    class Partial(FakeConnector):
        async def pull_ptu_utilization(self, account_id, deployments, start, end):
            rows = await super().pull_ptu_utilization(account_id, deployments, start, end)
            return [r.model_copy(update={"utilization": Decimal("0.5")}) for r in rows[:1]]

    # A narrower window only touches its own hours.
    n = await ptu.ingest_ptu(
        session, tenant_id, Partial(), account_id, deployments, start, start + timedelta(hours=2)
    )
    assert n == 1
    rows = await _rows(session)
    assert [(r[2].hour, r[3]) for r in rows][0] == (10, Decimal("0.5000"))
    assert [r[2].hour for r in rows] == [10, 12]


async def test_backfill_30_days_in_chunks(engine, session, tenant_id):
    fake = FakeConnector()
    await sync_inventory(session, tenant_id, fake)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    n = await ptu.backfill_ptu(factory, _settings(), now=NOW, connector=fake)
    assert n == 30 * 24 + 1  # 30 days back from the current hour, inclusive
    count = await session.scalar(select(func.count()).select_from(PtuUtilization))
    assert count == n
    # Re-running the backfill and then the hourly pass leaves the same rows.
    await ptu.backfill_ptu(factory, _settings(), now=NOW, connector=fake)
    await ptu.run_ptu(factory, _settings(), now=NOW, connector=fake)
    assert await session.scalar(select(func.count()).select_from(PtuUtilization)) == n


async def test_run_ptu_without_tenant_or_ptu_is_a_noop(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    assert await ptu.run_ptu(factory, _settings(), now=NOW, connector=FakeConnector()) == 0


# --- report ----------------------------------------------------------------------------


def _dep(tenant_id, name, sku, capacity, resource=RES, **kw) -> DeploymentRow:
    return DeploymentRow(
        tenant_id=tenant_id,
        cloud="azure",
        account_id="s",
        resource_id=resource,
        name=name,
        model="gpt-4o",
        sku=sku,
        capacity=capacity,
        **kw,
    )


def _line(tenant_id, meter, cost, day=date(2026, 10, 12), resource=RES, **kw) -> CostLine:
    return CostLine(
        tenant_id=tenant_id,
        cloud="azure",
        sub_account_id="s",
        resource_id=resource,
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
        **kw,
    )


def _util(tenant_id, name, hour, value, resource=RES) -> PtuUtilization:
    return PtuUtilization(
        tenant_id=tenant_id,
        cloud="azure",
        account_id="s",
        resource_id=resource,
        deployment=name,
        bucket_start=datetime(2026, 10, 12, hour, tzinfo=UTC),
        utilization=Decimal(value),
    )


async def test_report_maths_split_by_capacity(session, tenant_id, engine):
    session.add_all(
        [
            _dep(tenant_id, "big", "ProvisionedManaged", 150),
            _dep(tenant_id, "small", "GlobalProvisionedManaged", 50),
            _dep(tenant_id, "paygo", "GlobalStandard", 10),  # not PTU: not in the report
            # PTU cost on the shared resource: one mapped meter, one unmapped, case differs.
            _line(tenant_id, "gpt-4o Provisioned Managed", "300", ai_direction="provisioned"),
            _line(tenant_id, PTU_METER, "100", resource=RES.lower()),
            _line(tenant_id, "gpt-4o-0806 Inp glbl", "7", ai_direction="input"),  # paygo
            _line(tenant_id, PTU_METER, "999", day=date(2026, 9, 1)),  # out of range
            _line(tenant_id, PTU_METER, "55", resource=OTHER),  # resource without PTU
            _line(
                tenant_id,
                "Provisioned Managed Reservation 1 Month",
                "2000",
                resource="",
                service_name="Foundry Models",
            ),
            _util(tenant_id, "big", 0, "0.0500"),
            _util(tenant_id, "big", 1, "0.4500"),
            _util(tenant_id, "big", 2, "0.8000"),
            _util(tenant_id, "big", 3, "0.1000"),  # exactly 10%: not idle
            _util(tenant_id, "small", 0, "0.0000"),
            _util(tenant_id, "small", 1, "0.0900"),
        ]
    )
    await session.commit()
    report = await ptu.ptu_report(session, tenant_id, date(2026, 10, 1), date(2026, 10, 14))
    assert report.currency == "USD"
    by_name = {d.deployment: d for d in report.deployments}
    assert set(by_name) == {"big", "small"}

    big = by_name["big"]
    assert big.avg_utilization == Decimal("0.3500")
    assert big.peak_utilization == Decimal("0.8000")
    assert (big.idle_hours, big.hours) == (1, 4)
    assert big.ptu_cost == Decimal("300")  # 400 x 150/200
    assert big.idle_cost == Decimal("195")  # 300 x (1 - 0.35)
    assert (big.capacity, big.model) == (150, "gpt-4o")

    small = by_name["small"]
    assert small.avg_utilization == Decimal("0.0450")
    assert (small.idle_hours, small.hours) == (2, 2)
    assert small.ptu_cost == Decimal("100")
    assert small.idle_cost == Decimal("95.5")

    (res,) = report.reservations
    assert (res.sku_meter, res.cost, res.day) == (
        "Provisioned Managed Reservation 1 Month",
        Decimal("2000"),
        date(2026, 10, 12),
    )


async def test_report_without_samples_or_capacity(session, tenant_id):
    other_res = RES + "-2"
    session.add_all(
        [
            _dep(tenant_id, "a", "ProvisionedManaged", None, resource=other_res),
            _dep(tenant_id, "b", "ProvisionedManaged", None, resource=other_res),
            _dep(
                tenant_id,
                "gone",
                "ProvisionedManaged",
                10,
                deleted_at=datetime(2026, 9, 1, tzinfo=UTC),
            ),
            _line(tenant_id, PTU_METER, "80", resource=other_res),
        ]
    )
    await session.commit()
    report = await ptu.ptu_report(session, tenant_id, date(2026, 10, 1), date(2026, 10, 14))
    rows = {d.deployment: d for d in report.deployments}
    assert set(rows) == {"a", "b"}  # deleted before the range and no samples: left out
    for d in rows.values():
        assert d.ptu_cost == Decimal("40")  # unknown capacities: equal split
        assert d.avg_utilization is None and d.idle_cost is None
        assert (d.idle_hours, d.hours) == (0, 0)
    assert report.reservations == []


async def test_report_is_tenant_scoped(session, tenant_id):
    from ai_cost_tracker.db.models import Tenant

    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    session.add_all(
        [
            _dep(other.id, "theirs", "ProvisionedManaged", 50),
            _util(other.id, "theirs", 0, "0.5"),
            _line(other.id, PTU_METER, "10"),
        ]
    )
    await session.commit()
    report = await ptu.ptu_report(session, tenant_id, date(2026, 10, 1), date(2026, 10, 14))
    assert report.deployments == [] and report.reservations == []


async def test_reservation_heuristic():
    assert ptu.looks_like_reservation("Provisioned Managed Reservation", None)
    assert ptu.looks_like_reservation("PTU 1 Year", "Reserved Instances")
    assert not ptu.looks_like_reservation(PTU_METER, "Foundry Models")
