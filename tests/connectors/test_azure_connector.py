"""AzureConnector end to end over httpx.MockTransport with a fake token provider."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ai_cost_tracker.connectors.azure import AzureConnector
from ai_cost_tracker.connectors.azure.client import ARM, ArmClient
from ai_cost_tracker.core.models import Deployment

FIXTURES = Path(__file__).parent.parent / "fixtures" / "azure"
SUB = "11111111-1111-1111-1111-111111111111"
RG = f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts"
EASTUS = f"{RG}/aoai-eastus"
SWC = f"{RG}/foundry-swc"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeTokens:
    async def token(self) -> str:
        return "fake-token"


class Recorder:
    """Routes requests to a handler and keeps them for assertions."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        self.requests.append(request)
        return self.handler(request)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.content]


def connector(
    handler, subscription_ids: list[str] | None = None
) -> tuple[AzureConnector, Recorder]:
    async def no_sleep(_: float) -> None:
        return None

    recorder = Recorder(handler)
    http = httpx.AsyncClient(base_url=ARM, transport=httpx.MockTransport(recorder))
    client = ArmClient(FakeTokens(), http=http, sleep=no_sleep, max_retries=3)
    return AzureConnector(subscription_ids=subscription_ids, client=client), recorder


def graph_pages(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    page = 2 if body["options"].get("$skipToken") else 1
    return httpx.Response(200, json=load(f"graph_subscriptions_page{page}.json"))


# --- inventory -------------------------------------------------------------------------


async def test_list_accounts_only_ai_subscriptions_across_pages():
    conn, rec = connector(graph_pages)
    accounts = await conn.list_accounts()
    assert [a.account_id for a in accounts] == [SUB, "33333333-3333-3333-3333-333333333333"]
    assert all(a.has_ai_resources for a in accounts)
    assert accounts[0].name == "contoso-ai-prod"
    first, second = rec.bodies()
    assert first["options"] == {"resultFormat": "objectArray", "$top": 1000}
    assert "subscriptions" not in first
    assert second["options"]["$skipToken"] == load("graph_subscriptions_page1.json")["$skipToken"]
    assert rec.requests[0].url.path == "/providers/Microsoft.ResourceGraph/resources"


async def test_list_accounts_with_filter_keeps_subscriptions_without_ai():
    conn, rec = connector(graph_pages, subscription_ids=["22222222-2222-2222-2222-222222222222"])
    accounts = await conn.list_accounts()
    assert [(a.account_id, a.has_ai_resources) for a in accounts] == [
        ("22222222-2222-2222-2222-222222222222", False)
    ]
    assert rec.bodies()[0]["subscriptions"] == ["22222222-2222-2222-2222-222222222222"]


async def test_list_accounts_filter_is_case_insensitive():
    conn, _ = connector(graph_pages, subscription_ids=[SUB.upper()])
    assert [a.account_id for a in await conn.list_accounts()] == [SUB]


async def test_list_deployments_paginates_and_merges_tags():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources"):
            assert json.loads(request.content)["subscriptions"] == [SUB]
            return httpx.Response(200, json=load("graph_accounts.json"))
        if path == f"{EASTUS}/deployments":
            page = 2 if "$skipToken" in str(request.url) else 1
            return httpx.Response(200, json=load(f"deployments_eastus_page{page}.json"))
        if path == f"{SWC}/deployments":
            return httpx.Response(200, json=load("deployments_swc.json"))
        raise AssertionError(f"unexpected {request.url}")

    conn, rec = connector(handler)
    deployments = {d.name: d for d in await conn.list_deployments(SUB)}
    assert set(deployments) == {"search-chat-gpt4o", "search-embed", "o3-mini"}

    chat = deployments["search-chat-gpt4o"]
    assert (chat.model, chat.model_version) == ("gpt-4o", "2024-08-06")
    assert (chat.sku, chat.capacity) == ("GlobalStandard", 450)
    assert chat.resource_id == EASTUS
    # Deployment tags override account tags; account-only tags carry through.
    assert chat.tags == {"team": "search", "env": "staging", "app": "chat"}
    assert deployments["search-embed"].tags == {"team": "search", "env": "prod"}
    assert deployments["o3-mini"].tags == {}  # account tags null
    assert deployments["o3-mini"].sku == "DataZoneStandard"
    deployment_calls = [r for r in rec.requests if r.method == "GET"]
    assert len(deployment_calls) == 3
    assert all(r.url.params["api-version"] == "2024-10-01" for r in deployment_calls)


# --- cost ------------------------------------------------------------------------------


async def test_pull_cost_follows_absolute_next_link():
    next_url = load("cost_page1.json")["properties"]["nextLink"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        if "skiptoken" in str(request.url):
            assert str(request.url) == next_url
            return httpx.Response(200, json=load("cost_page2.json"))
        assert request.url.path == f"/subscriptions/{SUB}/providers/Microsoft.CostManagement/query"
        return httpx.Response(200, json=load("cost_page1.json"))

    conn, rec = connector(handler)
    records = await conn.pull_cost(SUB, date(2026, 10, 12), date(2026, 10, 13))
    assert [r.charge_period_start for r in records] == [
        date(2026, 10, 12),
        date(2026, 10, 12),
        date(2026, 10, 13),
    ]
    assert sum(r.billed_cost for r in records) == Decimal("4.747167")
    assert all(r.sub_account_id == SUB for r in records)
    # The same body is re-sent to nextLink.
    first, second = rec.bodies()
    assert first == second


async def test_pull_cost_splits_into_calendar_months():
    empty = {"properties": {"columns": [{"name": "Cost"}, {"name": "UsageDate"}], "rows": []}}
    conn, rec = connector(lambda r: httpx.Response(200, json=empty))
    await conn.pull_cost(SUB, date(2026, 8, 20), date(2026, 10, 3))
    periods = [b["timePeriod"] for b in rec.bodies()]
    assert periods == [
        {"from": "2026-08-20T00:00:00Z", "to": "2026-08-31T23:59:59Z"},
        {"from": "2026-09-01T00:00:00Z", "to": "2026-09-30T23:59:59Z"},
        {"from": "2026-10-01T00:00:00Z", "to": "2026-10-03T23:59:59Z"},
    ]


async def test_pull_cost_falls_back_to_pretax_cost_column():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["dataset"]["aggregation"]["totalCost"]["name"] == "Cost":
            return httpx.Response(400, json={"error": {"code": "BadRequest"}})
        return httpx.Response(200, json=load("cost_payg.json"))

    conn, rec = connector(handler)
    records = await conn.pull_cost(SUB, date(2026, 9, 30), date(2026, 10, 1))
    assert len(records) == 4  # two month chunks, two rows each
    names = [b["dataset"]["aggregation"]["totalCost"]["name"] for b in rec.bodies()]
    assert names == ["Cost", "PreTaxCost", "PreTaxCost"]  # remembered for the second month


async def test_pull_cost_429_then_success():
    responses = iter(
        [
            httpx.Response(
                429, headers={"x-ms-ratelimit-microsoft.costmanagement-entity-retry-after": "1"}
            ),
            httpx.Response(200, json=load("cost_page2.json")),
        ]
    )
    conn, rec = connector(lambda r: next(responses))
    records = await conn.pull_cost(SUB, date(2026, 10, 13), date(2026, 10, 13))
    assert len(records) == 1
    assert len(rec.requests) == 2


async def test_pull_cost_raises_after_retries():
    conn, rec = connector(lambda r: httpx.Response(429, headers={"Retry-After": "0"}))
    with pytest.raises(httpx.HTTPStatusError):
        await conn.pull_cost(SUB, date(2026, 10, 13), date(2026, 10, 13))
    assert len(rec.requests) == 3


# --- usage -----------------------------------------------------------------------------


def _deployment(name: str, model: str, resource_id: str) -> Deployment:
    return Deployment(
        cloud="azure", account_id=SUB, resource_id=resource_id, name=name, model=model
    )


async def test_pull_usage_hourly_per_deployment_with_encoded_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{EASTUS}/providers/Microsoft.Insights/metrics":
            return httpx.Response(200, json=load("metrics_eastus.json"))
        return httpx.Response(200, json={"value": []})

    deployments = [
        _deployment("search-chat-gpt4o", "gpt-4o", EASTUS),
        _deployment("search-embed", "text-embedding-3-small", EASTUS),
        _deployment("o3-mini", "o3-mini", SWC),
    ]
    conn, rec = connector(handler)
    usage = await conn.pull_usage(
        SUB,
        deployments,
        datetime(2026, 10, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 12, 2, tzinfo=UTC),
    )
    # One call per account (both fit in one chunk).
    assert len(rec.requests) == 2
    req = rec.requests[0]
    assert req.url.params["$filter"] == "ModelDeploymentName eq '*'"
    raw = req.url.query.decode()
    assert " " not in raw and "%27" in raw  # spaces and quotes are percent-encoded
    assert (
        req.url.params["metricnames"] == "ProcessedPromptTokens,GeneratedTokens,AzureOpenAIRequests"
    )

    totals = {
        (u.deployment, u.bucket_start.hour): (u.input_tokens, u.output_tokens, u.requests)
        for u in usage
    }
    assert totals[("search-chat-gpt4o", 0)] == (120000, 15000, 240)
    assert totals[("search-chat-gpt4o", 1)] == (98000, 0, 190)
    assert totals[("search-embed", 0)] == (500000, 0, 1000)
    assert ("new-unsynced", 1) in totals


async def test_pull_usage_chunks_long_ranges_to_seven_days():
    conn, rec = connector(lambda r: httpx.Response(200, json={"value": []}))
    await conn.pull_usage(
        SUB,
        [_deployment("search-chat-gpt4o", "gpt-4o", EASTUS)],
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    spans = [r.url.params["timespan"] for r in rec.requests]
    assert spans[0] == "2026-09-01T00:00:00Z/2026-09-08T00:00:00Z"
    assert spans[-1] == "2026-09-29T00:00:00Z/2026-10-01T00:00:00Z"
    assert len(spans) == 5


# --- discovery and access --------------------------------------------------------------


async def test_list_accounts_include_all_ignores_filter_and_keeps_non_ai():
    conn, rec = connector(graph_pages, subscription_ids=[SUB])
    accounts = await conn.list_accounts(include_all=True)
    assert [(a.account_id, a.has_ai_resources) for a in accounts] == [
        (SUB, True),
        ("22222222-2222-2222-2222-222222222222", False),
        ("33333333-3333-3333-3333-333333333333", True),
    ]
    assert all("subscriptions" not in b for b in rec.bodies())


@pytest.mark.parametrize("status,expected", [(200, True), (403, False), (404, False)])
async def test_check_access(status: int, expected: bool):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/subscriptions/{SUB}"
        assert request.url.params["api-version"] == "2022-12-01"
        body = {"subscriptionId": SUB, "state": "Enabled"} if status == 200 else {"error": {}}
        return httpx.Response(status, json=body)

    conn, rec = connector(handler)
    assert await conn.check_access(SUB) is expected
    assert len(rec.requests) == 1  # 403/404 are answers, not retried


async def test_check_access_server_error_raises_after_retries():
    conn, rec = connector(lambda r: httpx.Response(500, json={"error": {}}))
    with pytest.raises(httpx.HTTPStatusError):
        await conn.check_access(SUB)
    assert len(rec.requests) == 3


async def test_list_deployments_region_from_account_location():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources"):
            assert "location" in json.loads(request.content)["query"]
            return httpx.Response(200, json=load("graph_accounts.json"))
        if path == f"{EASTUS}/deployments":
            page = 2 if "$skipToken" in str(request.url) else 1
            return httpx.Response(200, json=load(f"deployments_eastus_page{page}.json"))
        return httpx.Response(200, json=load("deployments_swc.json"))

    conn, _ = connector(handler)
    regions = {d.name: d.region for d in await conn.list_deployments(SUB)}
    assert regions == {
        "search-chat-gpt4o": "eastus",
        "search-embed": "eastus",
        "o3-mini": "swedencentral",
    }
