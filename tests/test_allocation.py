"""Allocation rules, splits, rounding, untagged report and the weekly alert (#33, #34)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import allocation as alloc
from ai_cost_tracker.allocation import Allocation, distribute, resolve
from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import (
    AlertEvent,
    AllocationRule,
    CostLine,
    DailyEstimate,
    DeploymentRow,
    SplitRule,
    Tenant,
)

_ARM = "resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts"
PROD = f"/subscriptions/sub-a/{_ARM}/oai-prod"
DEV = f"/subscriptions/sub-b/{_ARM}/oai-dev"
ALL_TAGS = {"team": "search", "app": "bot", "env": "prod", "cost-centre": "CC1"}


def _dep(name: str, tags: dict | None = None, account: str = "sub-a", resource: str = PROD):
    return DeploymentRow(
        account_id=account, resource_id=resource, name=name, model="gpt-4o", tags=tags or {}
    )


def _rule(name, match_type, value, key=None, priority=100, active=True, **labels):
    return AllocationRule(
        name=name,
        priority=priority,
        match_type=match_type,
        match_key=key,
        match_value=value,
        active=active,
        cost_centre=labels.get("cost_centre"),
        team=labels.get("team"),
        app=labels.get("app"),
    )


# --- resolve --------------------------------------------------------------------------------


def test_no_rule_is_unallocated():
    assert resolve(_dep("chat"), "oai-prod", [], None) == [Allocation(None, None, None, Decimal(1))]


def test_tag_rule_key_case_insensitive_value_exact():
    rule = _rule("r", "tag", "Search", key="team", cost_centre="CC-S")
    assert resolve(_dep("x", {"Team": "Search"}), "oai-prod", [rule], None)[0].cost_centre == (
        "CC-S"
    )
    assert resolve(_dep("x", {"team": "search"}), "oai-prod", [rule], None)[0].cost_centre is None


def test_name_pattern_is_case_insensitive_glob_on_resource_and_deployment():
    rule = _rule("r", "name_pattern", "OAI-PROD/search-*", team="search")
    assert resolve(_dep("Search-Bot"), "oai-prod", [rule], None)[0].team == "search"
    assert resolve(_dep("search-bot"), "oai-dev", [rule], None)[0].team is None


def test_account_rule_matches_subscription():
    rule = _rule("r", "account", "sub-b", cost_centre="DEV")
    assert resolve(_dep("d", account="sub-b"), "oai-dev", [rule], None)[0].cost_centre == "DEV"
    assert resolve(_dep("d"), "oai-prod", [rule], None)[0].cost_centre is None


def test_priority_then_name_first_match_wins_and_inactive_skipped():
    dep = _dep("chat", {"team": "search"})
    rules = [
        _rule("b", "account", "sub-a", priority=10, cost_centre="B"),
        _rule("a", "account", "sub-a", priority=10, cost_centre="A"),
        _rule("z", "tag", "search", key="team", priority=1, cost_centre="OFF", active=False),
        _rule("first", "tag", "search", key="team", priority=50, cost_centre="LATE"),
    ]
    assert resolve(dep, "oai-prod", rules, None)[0].cost_centre == "A"


def test_split_overrides_rules():
    split = SplitRule(
        resource_id=PROD,
        deployment="shared",
        active=True,
        allocations=[
            {"cost_centre": "CC1", "team": "a", "app": "x", "percent": 70},
            {"cost_centre": "CC2", "team": "b", "app": None, "percent": "30"},
        ],
    )
    rule = _rule("r", "account", "sub-a", cost_centre="RULE")
    got = resolve(_dep("shared"), "oai-prod", [rule], split)
    assert got == [
        Allocation("CC1", "a", "x", Decimal("0.7")),
        Allocation("CC2", "b", None, Decimal("0.3")),
    ]
    split.active = False
    assert resolve(_dep("shared"), "oai-prod", [rule], split)[0].cost_centre == "RULE"


# --- distribute -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount,weights",
    [
        (Decimal("10"), [Decimal(1), Decimal(1), Decimal(1)]),
        (Decimal("0.0000000001"), [Decimal(1), Decimal(1)]),
        (Decimal("1.2345678901234567"), [Decimal("0.333"), Decimal("0.333"), Decimal("0.334")]),
        (Decimal("-7.01"), [Decimal("0.5"), Decimal("0.25"), Decimal("0.25")]),
        (Decimal("100"), [Decimal("0.7"), Decimal("0.3")]),
    ],
)
def test_distribute_sums_exactly(amount, weights):
    pieces = distribute(amount, weights)
    assert sum(pieces, Decimal(0)) == amount
    assert len(pieces) == len(weights)


def test_distribute_largest_remainder():
    assert distribute(Decimal("0.0000000002"), [Decimal(1)] * 3) == [
        Decimal("0.0000000001"),
        Decimal("0.0000000001"),
        Decimal("0E-10"),
    ]
    assert distribute(Decimal("10"), [Decimal(1)] * 3) == [
        Decimal("3.3333333334"),
        Decimal("3.3333333333"),
        Decimal("3.3333333333"),
    ]


# --- aggregation, report, weekly job --------------------------------------------------------


def _cost(t, account, resource, day, meter, model, cost) -> CostLine:
    return CostLine(
        tenant_id=t,
        cloud="azure",
        sub_account_id=account,
        resource_id=resource,
        charge_period_start=day,
        sku_meter=meter,
        billed_cost=Decimal(cost),
        billing_currency="USD",
        ai_model=model,
        ai_direction="input",
    )


def _est(t, account, resource, deployment, day, cost) -> DailyEstimate:
    return DailyEstimate(
        tenant_id=t,
        cloud="azure",
        account_id=account,
        resource_id=resource,
        deployment=deployment,
        model="gpt-4o",
        day=day,
        estimated_cost=Decimal(cost),
        currency="USD",
    )


D1, D2 = date(2026, 10, 5), date(2026, 10, 6)  # a Monday and Tuesday (ISO week 41)


@pytest.fixture
async def seeded(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    t = tenant_id
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    session.add_all(
        [
            DeploymentRow(
                tenant_id=t,
                cloud="azure",
                account_id="sub-a",
                resource_id=PROD,
                name="chat",
                model="gpt-4o",
                tags=dict(ALL_TAGS),
            ),
            DeploymentRow(
                tenant_id=t,
                cloud="azure",
                account_id="sub-a",
                resource_id=PROD,
                name="mini",
                model="gpt-4o-mini",
                tags={"team": "search", "env": " "},
            ),
            DeploymentRow(
                tenant_id=t,
                cloud="azure",
                account_id="sub-b",
                resource_id=DEV,
                name="dev",
                model="gpt-4o",
                tags={},
            ),
            DeploymentRow(
                tenant_id=t,
                cloud="azure",
                account_id="sub-b",
                resource_id=DEV,
                name="gone",
                model="gpt-4",
                tags={},
                deleted_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            # D1 billed for sub-a: 10 to chat, 3 to mini, 0.5 unattributed.
            _cost(t, "sub-a", PROD, D1, "gpt-4o In", "gpt-4o", "10"),
            _cost(t, "sub-a", PROD, D1, "mini In", "gpt-4o-mini", "3"),
            _cost(t, "sub-a", PROD, D1, "Mystery", None, "0.5"),
            # sub-b only estimated.
            _est(t, "sub-b", DEV, "dev", D1, "2"),
            _est(t, "sub-b", DEV, "dev", D2, "1"),
            _cost(other.id, "sub-a", PROD, D1, "gpt-4o In", "gpt-4o", "999"),
        ]
    )
    await session.commit()
    return t


async def test_cost_centres_daily_rules_split_and_unallocated(session, seeded):
    session.add_all(
        [
            AllocationRule(
                tenant_id=seeded,
                name="tag",
                priority=1,
                match_type="tag",
                match_key="cost-centre",
                match_value="CC1",
                cost_centre="CC1",
            ),
            SplitRule(
                tenant_id=seeded,
                resource_id=DEV,
                deployment="dev",
                allocations=[
                    {"cost_centre": "A", "percent": "33.3333"},
                    {"cost_centre": "B", "percent": "66.6667"},
                ],
            ),
        ]
    )
    await session.commit()
    rows = await alloc.cost_centres_daily(session, seeded, D1, D2)
    got = {(r.day, r.key): (r.cost, r.approximate) for r in rows}
    assert got[(D1, "CC1")] == (Decimal(10), False)
    assert got[(D1, alloc.UNALLOCATED)] == (Decimal("3.5"), True)
    assert got[(D1, "A")][1] and got[(D1, "B")][1]
    assert got[(D1, "A")][0] + got[(D1, "B")][0] == Decimal(2)
    assert got[(D2, "A")][0] + got[(D2, "B")][0] == Decimal(1)
    assert sum(r.cost for r in rows if r.day == D1) == Decimal("15.5")


async def test_cost_centres_daily_rejects_bad_level(session, seeded):
    with pytest.raises(ValueError):
        await alloc.cost_centres_daily(session, seeded, D1, D2, level="nope")


def test_missing_tags_case_insensitive_and_blank():
    assert alloc.missing_tags({"Team": "x", "APP": "y", "env": "", "cost-centre": "c"}) == ["env"]
    assert alloc.missing_tags(None) == alloc.REQUIRED_TAGS


async def test_untagged_report(session, seeded):
    report = await alloc.untagged_report(session, seeded, D1, D2)
    names = [(d.name, d.cost, d.missing_tags) for d in report.deployments]
    # chat is fully tagged; gone is deleted with no cost in range. Ties sort by resource.
    assert names == [
        ("dev", Decimal(3), alloc.REQUIRED_TAGS),
        ("mini", Decimal(3), ["app", "env", "cost-centre"]),
    ]
    assert report.total == Decimal(6)
    assert report.unattributed_cost == Decimal("0.5")
    assert report.deployments[1].account_name == "oai-prod"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


async def test_weekly_alert_mondays_only_and_idempotent(engine, session, seeded):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tuesday = datetime(2026, 10, 13, 6, tzinfo=UTC)
    assert await alloc.run_untagged_weekly(maker, _settings(), now=tuesday) == 0
    monday = datetime(2026, 10, 12, 6, tzinfo=UTC)
    assert await alloc.run_untagged_weekly(maker, _settings(), now=monday) == 2
    assert await alloc.run_untagged_weekly(maker, _settings(), now=monday) == 2
    alerts = (await session.scalars(select(AlertEvent))).all()
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.kind, a.subject, a.severity) == ("untagged_weekly", "2026-W41", "info")
    assert a.details["total_cost"] == "6.0000000000"
    assert [d["deployment"] for d in a.details["deployments"]] == ["oai-dev/dev", "oai-prod/mini"]

    # Notified, then re-run with nothing new: stays notified. A new untagged one resets it.
    async with maker() as s:
        row = await s.get(AlertEvent, a.id)
        row.notified_at = monday
        await s.commit()
    await alloc.run_untagged_weekly(maker, _settings(), now=monday)
    async with maker() as s:
        assert (await s.get(AlertEvent, a.id)).notified_at is not None
        s.add(
            DeploymentRow(
                tenant_id=seeded,
                cloud="azure",
                account_id="sub-b",
                resource_id=DEV,
                name="new",
                model="gpt-4o",
                tags={},
            )
        )
        await s.commit()
    assert await alloc.run_untagged_weekly(maker, _settings(), now=monday) == 3
    async with maker() as s:
        assert (await s.get(AlertEvent, a.id)).notified_at is None
        assert len((await s.scalars(select(AlertEvent))).all()) == 1


async def test_weekly_alert_skipped_when_everything_tagged(engine, session, tenant_id):
    session.add(
        DeploymentRow(
            tenant_id=tenant_id,
            cloud="azure",
            account_id="sub-a",
            resource_id=PROD,
            name="chat",
            model="gpt-4o",
            tags=dict(ALL_TAGS),
        )
    )
    await session.commit()
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monday = datetime(2026, 10, 12, 6, tzinfo=UTC)
    assert await alloc.run_untagged_weekly(maker, _settings(), now=monday) == 0
    assert (await session.scalars(select(AlertEvent))).all() == []
