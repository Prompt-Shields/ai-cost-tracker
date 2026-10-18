"""Azure connector: Resource Graph for inventory, Cost Management for cost, Monitor for metrics.

Needs only the ``Reader`` role on each subscription (ARCHITECTURE.md §4.3). Request and
response shapes follow Microsoft's REST docs; anything not yet seen on a live pay-as-you-go
subscription is marked as such and confirmed by the phase 0 spike (issue #2).
"""

from datetime import date, datetime
from typing import Any

import httpx

from ai_cost_tracker.connectors.azure import cost_query, metrics
from ai_cost_tracker.connectors.azure.client import ArmClient, TokenProvider
from ai_cost_tracker.connectors.base import PtuSample, is_provisioned
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage

RESOURCE_GRAPH_URL = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
DEPLOYMENTS_API_VERSION = "2024-10-01"
SUBSCRIPTION_API_VERSION = "2022-12-01"
GRAPH_PAGE_SIZE = 1000  # Resource Graph's maximum $top; the default is 100.

# Account kinds that host model deployments. Assumption (#2): other Cognitive Services kinds
# (Speech, TextAnalytics, ...) have no deployments and would only add empty ARM calls.
AI_ACCOUNT_KINDS = "('OpenAI', 'AIServices')"

# Every subscription the identity can see, flagged if it holds AI resources.
# Assumption (#2): $skipToken paging works for this join; small tenants fit in one page.
_SUBSCRIPTIONS_QUERY = f"""
resourcecontainers
| where type == 'microsoft.resources/subscriptions'
| project subscriptionId, name
| join kind=leftouter (
    resources
    | where type == 'microsoft.cognitiveservices/accounts' and kind in~ {AI_ACCOUNT_KINDS}
    | summarize aiAccounts = count() by subscriptionId
  ) on subscriptionId
| project subscriptionId, name, aiAccounts = coalesce(aiAccounts, 0)
| order by subscriptionId asc
"""

_ACCOUNTS_QUERY = f"""
resources
| where type == 'microsoft.cognitiveservices/accounts' and kind in~ {AI_ACCOUNT_KINDS}
| project id, name, kind, location, tags
| order by id asc
"""


class AzureConnector:
    name = "azure"

    def __init__(self, subscription_ids: list[str] | None = None, client: Any = None) -> None:
        self._only = {s.lower() for s in subscription_ids or []}
        self._client = client or ArmClient(TokenProvider())
        self._cost_column = cost_query.DEFAULT_COST_COLUMN

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        """Configured subscriptions if any, else every visible subscription with AI resources.

        ``include_all`` returns every visible subscription, with or without AI resources and
        ignoring the configured filter, for daily discovery.
        """
        if include_all:
            return [_account(r) for r in await self._graph(_SUBSCRIPTIONS_QUERY)]
        rows = await self._graph(_SUBSCRIPTIONS_QUERY, sorted(self._only) or None)
        accounts = [_account(r) for r in rows]
        if self._only:
            return [a for a in accounts if a.account_id.lower() in self._only]
        return [a for a in accounts if a.has_ai_resources]

    async def check_access(self, account_id: str) -> bool:
        """GET the subscription as the managed identity: 403/404 mean no role (yet).

        Assumption (#2): with no role on a subscription ARM answers 403
        (AuthorizationFailed) or 404 (SubscriptionNotFound) depending on whether the identity
        can see the subscription at all; any role on it, including Reader, allows this read.
        """
        try:
            await self._client.get(
                f"/subscriptions/{account_id}", params={"api-version": SUBSCRIPTION_API_VERSION}
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 404):
                return False
            raise
        return True

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        """Deployments of every AI account; account tags merged under deployment tags."""
        deployments = []
        for acct in await self._graph(_ACCOUNTS_QUERY, [account_id]):
            url: str | None = f"{acct['id']}/deployments?api-version={DEPLOYMENTS_API_VERSION}"
            while url:
                payload = await self._client.get(url)
                deployments.extend(
                    _deployment(account_id, acct, i) for i in payload.get("value") or []
                )
                url = payload.get("nextLink")
        return deployments

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        records: list[CostRecord] = []
        for chunk_start, chunk_end in cost_query.month_chunks(start, end):
            records.extend(await self._cost_chunk(account_id, chunk_start, chunk_end))
        return records

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        by_key: dict[tuple[str, str, datetime], TokenUsage] = {}
        for resource_id in sorted({d.resource_id for d in deployments}):
            for chunk_start, chunk_end in metrics.time_chunks(start, end):
                path, params = metrics.metrics_request(resource_id, chunk_start, chunk_end)
                payload = await self._client.get(path, params=params)
                for u in metrics.parse_metrics(account_id, resource_id, deployments, payload):
                    # Chunks don't overlap, but a boundary bucket could appear in both.
                    key = (u.resource_id, u.deployment, u.bucket_start)
                    by_key.setdefault(key, u)
        return list(by_key.values())

    async def pull_ptu_utilization(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[PtuSample]:
        """Hourly PTU utilisation from Monitor; one call per account and ≤7-day chunk."""
        wanted: dict[str, set[str]] = {}
        for d in deployments:
            if is_provisioned(d.sku):
                wanted.setdefault(d.resource_id, set()).add(d.name)
        by_key: dict[tuple[str, str, datetime], PtuSample] = {}
        for resource_id in sorted(wanted):
            for chunk_start, chunk_end in metrics.time_chunks(start, end):
                path, params = metrics.ptu_request(resource_id, chunk_start, chunk_end)
                payload = await self._client.get(path, params=params)
                for s in metrics.parse_ptu(resource_id, wanted[resource_id], payload):
                    by_key.setdefault((s.resource_id, s.deployment, s.bucket_start), s)
        return list(by_key.values())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _cost_chunk(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        try:
            return await self._cost_pages(account_id, start, end)
        except httpx.HTTPStatusError as exc:
            # Assumption (#2): legacy pay-as-you-go scopes reject the "Cost" column with a 400
            # and want "PreTaxCost". Switch once and remember for later chunks.
            if exc.response.status_code != 400 or self._cost_column != "Cost":
                raise
            self._cost_column = cost_query.PAYG_COST_COLUMN
            return await self._cost_pages(account_id, start, end)

    async def _cost_pages(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        url: str | None = cost_query.query_url(account_id)
        body = cost_query.build_query(start, end, self._cost_column)
        records: list[CostRecord] = []
        while url:
            # nextLink is absolute and must receive the same body again.
            payload = await self._client.post(url, json=body)
            records.extend(cost_query.parse_rows(account_id, payload))
            url = cost_query.next_link(payload)
        return records

    async def _graph(self, query: str, subscriptions: list[str] | None = None) -> list[dict]:
        options: dict[str, Any] = {"resultFormat": "objectArray", "$top": GRAPH_PAGE_SIZE}
        body: dict[str, Any] = {"query": query, "options": options}
        if subscriptions:
            body["subscriptions"] = subscriptions
        rows: list[dict] = []
        while True:
            payload = await self._client.post(RESOURCE_GRAPH_URL, json=body)
            rows.extend(payload.get("data") or [])
            token = payload.get("$skipToken")
            if not token:
                return rows
            body = {**body, "options": {**options, "$skipToken": token}}


def _account(row: dict[str, Any]) -> CloudAccount:
    return CloudAccount(
        cloud="azure",
        account_id=row["subscriptionId"],
        name=row["name"],
        has_ai_resources=(row.get("aiAccounts") or 0) > 0,
    )


def _deployment(account_id: str, acct: dict[str, Any], item: dict[str, Any]) -> Deployment:
    props = item.get("properties") or {}
    model = props.get("model") or {}
    sku = item.get("sku") or {}
    return Deployment(
        cloud="azure",
        account_id=account_id,
        resource_id=acct["id"],
        name=item["name"],
        model=model.get("name") or "unknown",
        model_version=model.get("version"),
        sku=sku.get("name"),
        capacity=sku.get("capacity"),
        # A deployment lives in its account's region. Assumption (#2): Global/DataZone SKUs,
        # which route traffic elsewhere, are still priced by the account's region.
        region=(acct.get("location") or "").lower() or None,
        tags={**(acct.get("tags") or {}), **(item.get("tags") or {})},
    )
