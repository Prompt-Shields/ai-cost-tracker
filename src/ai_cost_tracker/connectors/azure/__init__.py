"""Azure connector: Resource Graph for inventory, Cost Management for cost, Monitor for tokens.

Needs only the ``Reader`` role on each subscription (ARCHITECTURE.md §4.3).
"""

from datetime import UTC, date, datetime
from typing import Any

from ai_cost_tracker.connectors.azure import cost_query, metrics
from ai_cost_tracker.connectors.azure.client import ArmClient, TokenProvider
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage

RESOURCE_GRAPH_URL = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
DEPLOYMENTS_API_VERSION = "2024-10-01"

# Every subscription the identity can see, flagged if it holds AI resources.
_SUBSCRIPTIONS_QUERY = """
resourcecontainers
| where type == 'microsoft.resources/subscriptions'
| project subscriptionId, name
| join kind=leftouter (
    resources
    | where type == 'microsoft.cognitiveservices/accounts'
    | summarize aiAccounts = count() by subscriptionId
  ) on subscriptionId
| project subscriptionId, name, aiAccounts = coalesce(aiAccounts, 0)
"""

_ACCOUNTS_QUERY = """
resources
| where type == 'microsoft.cognitiveservices/accounts'
| project id, name, tags
"""


class AzureConnector:
    name = "azure"

    def __init__(self, subscription_ids: list[str] | None = None, client: Any = None) -> None:
        self._only = set(subscription_ids or [])
        self._client = client or ArmClient(TokenProvider())

    async def list_accounts(self) -> list[CloudAccount]:
        rows = await self._graph(_SUBSCRIPTIONS_QUERY)
        accounts = [
            CloudAccount(
                cloud="azure",
                account_id=r["subscriptionId"],
                name=r["name"],
                has_ai_resources=r.get("aiAccounts", 0) > 0,
            )
            for r in rows
        ]
        if self._only:
            return [a for a in accounts if a.account_id in self._only]
        return [a for a in accounts if a.has_ai_resources]

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        deployments = []
        for acct in await self._graph(_ACCOUNTS_QUERY, [account_id]):
            payload = await self._client.get(
                f"{acct['id']}/deployments?api-version={DEPLOYMENTS_API_VERSION}"
            )
            for item in payload.get("value", []):
                props = item.get("properties", {})
                model = props.get("model", {})
                sku = item.get("sku", {})
                deployments.append(
                    Deployment(
                        cloud="azure",
                        account_id=account_id,
                        resource_id=acct["id"],
                        name=item["name"],
                        model=model.get("name", "unknown"),
                        model_version=model.get("version"),
                        sku=sku.get("name"),
                        capacity=sku.get("capacity"),
                        tags={**(acct.get("tags") or {}), **(item.get("tags") or {})},
                    )
                )
        return deployments

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        url: str | None = cost_query.query_url(account_id)
        body = cost_query.build_query(start, end)
        records: list[CostRecord] = []
        while url:
            payload = await self._client.post(url, json=body)
            records.extend(cost_query.parse_rows(account_id, payload))
            url = cost_query.next_link(payload)
        return records

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        usage: list[TokenUsage] = []
        for resource_id in sorted({d.resource_id for d in deployments}):
            payload = await self._client.get(
                metrics.metrics_url(resource_id, start.astimezone(UTC), end.astimezone(UTC))
            )
            usage.extend(metrics.parse_metrics(account_id, resource_id, deployments, payload))
        return usage

    async def _graph(self, query: str, subscriptions: list[str] | None = None) -> list[dict]:
        body: dict[str, Any] = {"query": query, "options": {"resultFormat": "objectArray"}}
        if subscriptions:
            body["subscriptions"] = subscriptions
        rows: list[dict] = []
        while True:
            payload = await self._client.post(RESOURCE_GRAPH_URL, json=body)
            rows.extend(payload.get("data", []))
            token = payload.get("$skipToken")
            if not token:
                return rows
            body["options"]["$skipToken"] = token
