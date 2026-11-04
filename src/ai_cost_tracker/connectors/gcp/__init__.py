"""GCP connector: BigQuery billing export for cost, Cloud Monitoring for tokens, Vertex AI for
inventory (ARCHITECTURE.md §8, phase 3).

Authenticates without keys: the app's Azure managed identity is exchanged for a Google token
through Workload Identity Federation and impersonates one service account (``auth.py``).
Each project grants that service account read-only roles; the billing export dataset grants
it BigQuery read access. Anything not yet seen on a live project is marked
``Assumption (spike-gcp)`` and checked by ``act spike-gcp``.
"""

from datetime import date, datetime
from typing import Any

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.connectors.gcp import billing, inventory, monitoring
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


class GcpConnector:
    name = "gcp"
    cloud = "gcp"

    def __init__(
        self,
        client: Any,
        *,
        project_ids: list[str] | None = None,
        billing_table: str = "",
        billing_location: str = "US",
        regions: list[str] | None = None,
    ) -> None:
        self._client = client
        self._only = {p.lower() for p in project_ids or []}
        self._billing_table = billing_table
        self._billing_location = billing_location
        self._regions = list(regions or ["global"])

    @classmethod
    def from_settings(cls, settings: Settings) -> "GcpConnector":
        return cls(
            client_from_settings(settings),
            project_ids=settings.gcp_project_ids,
            billing_table=settings.gcp_billing_export_table,
            billing_location=settings.gcp_billing_export_location,
            regions=settings.gcp_regions,
        )

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        """Configured projects if any, else visible projects that use Vertex AI.

        ``include_all`` returns every visible project, ignoring the filter, for discovery.
        """
        return await inventory.list_projects(
            self._client, include_all=include_all, only=set() if include_all else self._only
        )

    async def check_access(self, account_id: str) -> bool:
        return await inventory.check_access(self._client, account_id)

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return await inventory.list_deployments(self._client, account_id, self._regions)

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        if not self._billing_table:
            raise RuntimeError("ACT_GCP_BILLING_EXPORT_TABLE is not set; GCP cost is unavailable")
        return await billing.pull_cost(
            self._client,
            table=self._billing_table,
            location=self._billing_location,
            project_id=account_id,
            start=start,
            end=end,
        )

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        return await monitoring.pull_usage(self._client, account_id, deployments, start, end)

    async def pull_ptu_utilization(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[PtuSample]:
        """Vertex AI provisioned throughput isn't modelled yet; nothing to report."""
        return []

    async def aclose(self) -> None:
        await self._client.aclose()


def client_from_settings(settings: Settings) -> Any:
    """A ``GcpClient`` authenticated through WIF with the configured service account."""
    from ai_cost_tracker.connectors.gcp.auth import WifTokenProvider
    from ai_cost_tracker.connectors.gcp.client import GcpClient

    return GcpClient(
        WifTokenProvider(
            audience=settings.gcp_wif_audience,
            service_account=settings.gcp_service_account,
            azure_scope=settings.gcp_azure_token_scope,
        )
    )


__all__ = ["GcpConnector", "client_from_settings"]
