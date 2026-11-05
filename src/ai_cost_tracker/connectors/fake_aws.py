"""Deterministic demo AWS account. Placeholder from the M4 foundation; W4 fills it in."""

from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


class FakeAwsConnector:
    name = "fake_aws"
    cloud = "aws"

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        return []

    async def check_access(self, account_id: str) -> bool:
        return True

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return []

    async def pull_cost(self, account_id, start, end) -> list[CostRecord]:
        return []

    async def pull_usage(self, account_id, deployments, start, end) -> list[TokenUsage]:
        return []

    async def pull_ptu_utilization(self, account_id, deployments, start, end) -> list[PtuSample]:
        return []
