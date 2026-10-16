from datetime import date, datetime
from typing import Protocol

from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


class Connector(Protocol):
    """What every cloud integration implements. Ingestion and the UI only see this."""

    name: str

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        """Accounts (subscriptions / projects) the connector should ingest.

        ``include_all=True`` ignores any configured account filter and returns every account
        the identity can see, each flagged with ``has_ai_resources``, so daily discovery can
        spot accounts nobody has chosen to track yet.
        """
        ...

    async def check_access(self, account_id: str) -> bool:
        """Whether the connector's identity can read ``account_id`` right now.

        False when the cloud says forbidden / not found (role not granted yet, or removed);
        any other failure raises, so a transient outage never flips an account's status.
        """
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
