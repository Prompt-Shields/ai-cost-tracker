"""AWS connector: CUR 2.0 Data Exports for cost, CloudWatch for tokens, Bedrock for inventory
(ARCHITECTURE.md §8, phase 4).

Authenticates without access keys: the app's Azure managed identity token is exchanged for
temporary AWS credentials with ``AssumeRoleWithWebIdentity`` into a read-only role that each
account creates from our CloudFormation template (``auth.py``). Anything not yet seen on a
live account is marked ``Assumption (spike-aws)`` and checked by ``act spike-aws``.
"""

from datetime import date, datetime
from typing import Any

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.aws import billing, inventory, metrics
from ai_cost_tracker.connectors.aws.ids import ExportLocation
from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


class AwsConnector:
    name = "aws"
    cloud = "aws"

    def __init__(
        self,
        sessions: Any,
        *,
        account_ids: list[str] | None = None,
        regions: list[str] | None = None,
        export: ExportLocation | None = None,
    ) -> None:
        self._sessions = sessions
        self._only = set(account_ids or [])
        self._regions = list(regions or ["us-east-1"])
        self._export = export

    @classmethod
    def from_settings(cls, settings: Settings) -> "AwsConnector":
        return cls(
            sessions_from_settings(settings),
            account_ids=settings.aws_account_ids,
            regions=settings.aws_regions,
            export=export_from_settings(settings),
        )

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        """Configured accounts if any, else organization accounts that use Bedrock.

        ``include_all`` returns every visible account, ignoring the filter, for discovery.
        """
        return await inventory.list_accounts(
            self._sessions,
            include_all=include_all,
            only=set() if include_all else self._only,
            export_account_id=self._export.account_id if self._export else None,
            regions=self._regions,
        )

    async def check_access(self, account_id: str) -> bool:
        return await inventory.check_access(self._sessions, account_id)

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return await inventory.list_deployments(self._sessions, account_id, self._regions)

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        if self._export is None:
            raise RuntimeError("ACT_AWS_EXPORT_BUCKET is not set; AWS cost is unavailable")
        return await billing.pull_cost(self._sessions, self._export, account_id, start, end)

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        return await metrics.pull_usage(
            self._sessions, account_id, self._regions, deployments, start, end
        )

    async def pull_ptu_utilization(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[PtuSample]:
        """Bedrock reports no utilisation for provisioned throughput; nothing to report."""
        return []

    async def aclose(self) -> None:
        await self._sessions.aclose()


def sessions_from_settings(settings: Settings) -> Any:
    """``AwsSessions`` that assume ``aws_role_name`` in each account with the app's identity."""
    from ai_cost_tracker.connectors.aws.auth import AwsSessions

    return AwsSessions(role_name=settings.aws_role_name, azure_scope=settings.aws_azure_token_scope)


def export_from_settings(settings: Settings) -> ExportLocation | None:
    if not (settings.aws_export_bucket and settings.aws_export_account_id):
        return None
    return ExportLocation(
        account_id=settings.aws_export_account_id,
        bucket=settings.aws_export_bucket,
        prefix=settings.aws_export_prefix.strip("/"),
        name=settings.aws_export_name,
        region=settings.aws_export_region,
    )


__all__ = ["AwsConnector", "export_from_settings", "sessions_from_settings"]
