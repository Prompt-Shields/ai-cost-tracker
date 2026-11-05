"""AWS wording in connection health and discovery alerts ("account", CloudFormation fixes)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.core.models import CloudAccount
from ai_cost_tracker.db.models import AlertEvent, CloudAccountRow, SyncState
from ai_cost_tracker.health import describe
from ai_cost_tracker.worker.discovery import check_pending_access, run_discovery

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
ACCOUNT = "123456789012"


def _state(error: str | None = None, ago: timedelta | None = None) -> SyncState:
    return SyncState(
        cloud="aws",
        account_id=ACCOUNT,
        source="usage",
        last_error=error,
        last_success_at=NOW - ago if ago else None,
    )


def test_aws_waiting_points_at_the_stack():
    h = describe("usage", None, NOW, "waiting_for_access", account_id=ACCOUNT, cloud="aws")
    assert h.status == "never"
    assert "CloudFormation" in h.message
    assert "ai-cost-tracker-readonly" in h.fix and ACCOUNT in h.fix
    assert "az " not in h.fix and "subscription" not in h.fix


def test_aws_no_access_names_role_and_account():
    h = describe(
        "cost", None, NOW, "no_access", account_id=ACCOUNT, cloud="aws", aws_role_name="MyRole"
    )
    assert h.status == "error"
    assert h.message == f"Can't assume role MyRole in account {ACCOUNT}"
    assert "MyRole" in h.fix and "CloudFormation" in h.fix


@pytest.mark.parametrize(
    ("error", "needle", "has_fix"),
    [
        ("AwsAccessDenied: AccessDenied when calling AssumeRoleWithWebIdentity", "assume", True),
        ("AwsAuthError: could not get an Azure token for the AWS scope", "exchange", True),
        ("ThrottlingException: Rate exceeded", "throttling", False),
        ("boom\ntraceback", "boom", False),
    ],
)
def test_aws_errors_explained(error, needle, has_fix):
    h = describe("usage", _state(error), NOW, "connected", cloud="aws")
    assert h.status == "error"
    assert needle in h.message.lower()
    assert (h.fix is not None) is has_fix
    assert "subscription" not in h.message and "az role" not in (h.fix or "")


def test_aws_freshness_like_other_clouds():
    assert (
        describe("usage", _state(ago=timedelta(minutes=5)), NOW, "connected", cloud="aws").status
        == "ok"
    )
    stale = describe("usage", _state(ago=timedelta(hours=2)), NOW, "connected", cloud="aws")
    assert stale.status == "stale"


def test_azure_default_unchanged():
    h = describe("usage", None, NOW, "no_access", account_id="sub-1")
    assert h.message == "Reader role missing on subscription sub-1"


class AwsStub:
    name = "stub_aws"
    cloud = "aws"

    def __init__(self) -> None:
        self.access = True

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        return [
            CloudAccount(cloud="aws", account_id=ACCOUNT, name="Research", has_ai_resources=True)
        ]

    async def check_access(self, account_id: str) -> bool:
        return self.access


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_discovery_says_account_and_cloudformation(factory, tenant_id):
    settings = Settings(default_tenant_slug="internal")
    stub = AwsStub()
    assert await run_discovery(factory, stub, settings) == [ACCOUNT]
    async with factory() as s:
        (alert,) = (await s.scalars(select(AlertEvent))).all()
        assert alert.message == "New account with AI resources found: Research"
        assert alert.details == {"cloud": "aws", "name": "Research"}
        row = await s.scalar(select(CloudAccountRow))
        row.tracked = True
        await s.commit()

    stub.access = False
    await check_pending_access(factory, stub, settings)
    async with factory() as s:
        lost = await s.scalar(select(AlertEvent).where(AlertEvent.kind == "access_lost"))
        assert lost.message == (
            "Lost access to Research: re-deploy the ai-cost-tracker-readonly CloudFormation stack"
        )
        state = await s.scalar(select(SyncState).where(SyncState.source == "access"))
        assert state.last_error == (
            "Read-only IAM role missing: the app can no longer read this account"
        )
