from datetime import date, datetime
from typing import Protocol

from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


class Connector(Protocol):
    """What every cloud integration implements. Ingestion and the UI only see this."""

    name: str

    async def list_accounts(self) -> list[CloudAccount]:
        """Accounts (subscriptions / projects) visible to the connector's identity."""
        ...

    async def list_deployments(self, account_id: str) -> list[Deployment]: ...

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        """Daily billed cost for ``start``..``end`` inclusive."""
        ...

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        """Hourly token usage for the given deployments in ``[start, end)``."""
        ...
