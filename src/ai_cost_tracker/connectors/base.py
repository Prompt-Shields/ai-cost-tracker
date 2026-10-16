from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field

from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage

# Deployment SKUs billed per hour of reserved capacity (PTU), compared case-insensitively.
PROVISIONED_SKUS = frozenset(
    {"provisionedmanaged", "globalprovisionedmanaged", "datazoneprovisionedmanaged"}
)


def is_provisioned(sku: str | None) -> bool:
    """Whether a deployment SKU is provisioned throughput (the only kind with utilisation)."""
    return (sku or "").replace(" ", "").lower() in PROVISIONED_SKUS


class PtuSample(BaseModel):
    """Utilisation of one provisioned deployment over one hour, as a 0..1 fraction.

    ``utilization`` is the hourly average; ``max_utilization`` the hourly peak where the cloud
    reports one. Only the average is stored (``ptu_utilization.utilization``).
    """

    resource_id: str
    deployment: str
    bucket_start: datetime
    utilization: Decimal = Field(ge=0, le=1)
    max_utilization: Decimal | None = Field(default=None, ge=0, le=1)


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

    async def pull_ptu_utilization(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[PtuSample]:
        """Hourly utilisation in ``[start, end)`` of the provisioned ones among ``deployments``.

        Non-provisioned deployments are ignored, so callers may pass the whole inventory.
        """
        ...
