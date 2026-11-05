"""AwsConnector composed over a fake ``AwsSessions``: tokens end to end through metrics.py,
cost and inventory delegated to their modules (monkeypatched here, tested by their owners)."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from ai_cost_tracker.connectors.aws import AwsConnector, billing, ids, inventory
from ai_cost_tracker.connectors.aws.auth import AwsAccessDenied
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment
from tests.connectors.test_aws_metrics import ACCOUNT, END, HAIKU, NOVA, SONNET, START, FakeSessions

EXPORT = ids.ExportLocation(
    account_id="999999999999",
    bucket="cur-bucket",
    prefix="ai-cost-tracker",
    name="ai-cost-tracker-cur2",
    region="us-east-1",
)


def connector(sessions: Any = None, **kwargs: Any) -> AwsConnector:
    kwargs.setdefault("regions", ["us-east-1", "eu-west-1"])
    kwargs.setdefault("export", EXPORT)
    return AwsConnector(sessions or FakeSessions(), **kwargs)


def dep(name: str, region: str) -> Deployment:
    return Deployment(
        cloud="aws",
        account_id=ACCOUNT,
        resource_id=ids.resource_id(ACCOUNT, region),
        name=name,
        model=f"inv-{name}",
        region=region,
    )


async def test_identity():
    conn = connector()
    assert (conn.name, conn.cloud) == ("aws", "aws")


async def test_pull_usage_end_to_end_over_configured_regions():
    deps = [dep(HAIKU, "us-east-1"), dep(NOVA, "eu-west-1")]
    usage = await connector().pull_usage(ACCOUNT, deps, START, END)
    assert {u.cloud for u in usage} == {"aws"}
    assert {(u.resource_id, u.deployment) for u in usage} == {
        (ids.resource_id(ACCOUNT, "us-east-1"), HAIKU),
        (ids.resource_id(ACCOUNT, "us-east-1"), SONNET),
        (ids.resource_id(ACCOUNT, "eu-west-1"), NOVA),
    }
    assert sum(u.input_tokens for u in usage) == 1200 + 800 + 7300 + 40
    assert sum(u.output_tokens for u in usage) == 150 + 90 + 700
    assert sum(u.requests for u in usage) == 6 + 4 + 3 + 1
    assert {u.deployment: u.model for u in usage}[NOVA] == f"inv-{NOVA}"
    assert await connector().pull_ptu_utilization(ACCOUNT, deps, START, END) == []


async def test_pull_usage_defaults_to_us_east_1():
    sessions = FakeSessions()
    usage = await AwsConnector(sessions).pull_usage(ACCOUNT, [], START, END)
    assert {region for region, _, _ in sessions.calls} == {"us-east-1"}
    assert {u.deployment for u in usage} == {HAIKU, SONNET}


async def test_pull_usage_denied_everywhere_raises():
    fail = {r: AwsAccessDenied("no") for r in ("us-east-1", "eu-west-1")}
    with pytest.raises(AwsAccessDenied):
        await connector(FakeSessions(fail=fail)).pull_usage(ACCOUNT, [], START, END)


async def test_pull_cost_delegates_to_billing(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, Any] = {}
    record = CostRecord(
        cloud="aws",
        sub_account_id=ACCOUNT,
        resource_id=ids.resource_id(ACCOUNT, "us-east-1"),
        service_name="Amazon Bedrock",
        sku_meter="Claude3.5Haiku-input-tokens",
        charge_period_start=date(2025, 3, 8),
        billed_cost=Decimal("1.25"),
        billing_currency="USD",
    )

    async def fake_pull_cost(sessions, export, account_id, start, end):
        seen.update(sessions=sessions, export=export, args=(account_id, start, end))
        return [record]

    monkeypatch.setattr(billing, "pull_cost", fake_pull_cost)
    sessions = FakeSessions()
    rows = await connector(sessions).pull_cost(ACCOUNT, date(2025, 3, 1), date(2025, 3, 9))
    assert rows == [record]
    assert seen == {
        "sessions": sessions,
        "export": EXPORT,
        "args": (ACCOUNT, date(2025, 3, 1), date(2025, 3, 9)),
    }


async def test_pull_cost_without_export_explains_setting():
    with pytest.raises(RuntimeError, match="ACT_AWS_EXPORT_BUCKET"):
        await connector(export=None).pull_cost(ACCOUNT, date(2025, 3, 1), date(2025, 3, 2))


async def test_inventory_calls_delegate(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, Any]] = []
    account = CloudAccount(cloud="aws", account_id=ACCOUNT, name="Demo")

    async def list_accounts(sessions, **kwargs):
        calls.append(("list_accounts", kwargs))
        return [account]

    async def check_access(sessions, account_id):
        calls.append(("check_access", account_id))
        return True

    async def list_deployments(sessions, account_id, regions):
        calls.append(("list_deployments", (account_id, regions)))
        return [dep(HAIKU, "us-east-1")]

    monkeypatch.setattr(inventory, "list_accounts", list_accounts)
    monkeypatch.setattr(inventory, "check_access", check_access)
    monkeypatch.setattr(inventory, "list_deployments", list_deployments)

    conn = connector(account_ids=[ACCOUNT])
    assert await conn.list_accounts() == [account]
    assert await conn.list_accounts(include_all=True) == [account]
    assert await conn.check_access(ACCOUNT) is True
    assert [d.name for d in await conn.list_deployments(ACCOUNT)] == [HAIKU]
    base = {"export_account_id": "999999999999", "regions": ["us-east-1", "eu-west-1"]}
    assert calls == [
        ("list_accounts", {"include_all": False, "only": {ACCOUNT}, **base}),
        ("list_accounts", {"include_all": True, "only": set(), **base}),
        ("check_access", ACCOUNT),
        ("list_deployments", (ACCOUNT, ["us-east-1", "eu-west-1"])),
    ]


async def test_aclose_closes_sessions():
    sessions = FakeSessions()
    await connector(sessions).aclose()
    assert sessions.closed
