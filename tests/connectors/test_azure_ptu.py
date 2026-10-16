"""Azure PTU utilisation (issue #32): Monitor request shape and parsing, over MockTransport."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from ai_cost_tracker.connectors.azure import AzureConnector, metrics
from ai_cost_tracker.connectors.azure.client import ARM, ArmClient
from ai_cost_tracker.core.models import Deployment

FIXTURES = Path(__file__).parent.parent / "fixtures" / "azure"
SUB = "11111111-1111-1111-1111-111111111111"
RG = f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts"
EASTUS = f"{RG}/aoai-eastus"
SWC = f"{RG}/foundry-swc"
START = datetime(2026, 10, 12, 0, tzinfo=UTC)
END = datetime(2026, 10, 12, 3, tzinfo=UTC)


class FakeTokens:
    async def token(self) -> str:
        return "fake-token"


def _connector(handler) -> tuple[AzureConnector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    async def no_sleep(_: float) -> None:
        return None

    http = httpx.AsyncClient(base_url=ARM, transport=httpx.MockTransport(record))
    client = ArmClient(FakeTokens(), http=http, sleep=no_sleep, max_retries=1)
    return AzureConnector(client=client), seen


def _dep(name: str, resource: str, sku: str) -> Deployment:
    return Deployment(
        cloud="azure", account_id=SUB, resource_id=resource, name=name, model="gpt-4o", sku=sku
    )


def _fixture() -> dict:
    return json.loads((FIXTURES / "ptu_eastus.json").read_text())


async def test_pull_ptu_utilization_parses_percent_to_fraction():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{EASTUS}/providers/Microsoft.Insights/metrics"
        return httpx.Response(200, json=_fixture())

    deployments = [
        _dep("batch-ptu", EASTUS, "ProvisionedManaged"),
        _dep("search-chat-gpt4o", EASTUS, "GlobalStandard"),  # not provisioned: ignored
        _dep("o3-mini", SWC, "DataZoneStandard"),  # no PTU on this account: no call
    ]
    conn, seen = _connector(handler)
    samples = await conn.pull_ptu_utilization(SUB, deployments, START, END)

    assert len(seen) == 1
    params = seen[0].url.params
    assert params["metricnames"] == metrics.PTU_UTILIZATION_METRIC
    assert params["aggregation"] == "Average,Maximum"
    assert params["interval"] == "PT1H"
    assert params["$filter"] == "ModelDeploymentName eq '*'"
    assert params["timespan"] == "2026-10-12T00:00:00Z/2026-10-12T03:00:00Z"

    # Monitor's "Batch-PTU" matches inventory's "batch-ptu"; the empty 02:00 point is skipped
    # (no data, not idle); 104% peak is clamped to 1.
    assert [
        (s.deployment, s.bucket_start.hour, s.utilization, s.max_utilization) for s in samples
    ] == [
        ("batch-ptu", 0, Decimal("0.0450"), Decimal("0.1200")),
        ("batch-ptu", 1, Decimal("0.6225"), Decimal("1.0000")),
    ]
    assert all(s.resource_id == EASTUS for s in samples)
    assert all(s.bucket_start.tzinfo is not None for s in samples)


async def test_pull_ptu_utilization_all_provisioned_skus_and_chunks():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"value": []})

    deployments = [
        _dep("a", EASTUS, "GlobalProvisionedManaged"),
        _dep("b", SWC, "DataZoneProvisionedManaged"),
    ]
    conn, _seen = _connector(handler)
    # Nine days: two 7-day-max chunks per account.
    out = await conn.pull_ptu_utilization(
        SUB, deployments, START, datetime(2026, 10, 21, 0, tzinfo=UTC)
    )
    assert out == []
    assert sorted(calls) == sorted(
        [f"{EASTUS}/providers/Microsoft.Insights/metrics"] * 2
        + [f"{SWC}/providers/Microsoft.Insights/metrics"] * 2
    )


async def test_pull_ptu_utilization_no_provisioned_makes_no_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no Monitor call expected")

    conn, _ = _connector(handler)
    assert await conn.pull_ptu_utilization(SUB, [_dep("x", EASTUS, "Standard")], START, END) == []


def test_parse_ptu_ignores_other_metrics_and_unknown_deployments():
    payload = _fixture()
    payload["value"].append({"name": {"value": "ProcessedPromptTokens"}, "timeseries": []})
    assert metrics.parse_ptu(EASTUS, {"other"}, payload) == []
