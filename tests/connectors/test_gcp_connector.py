"""GcpConnector composed end to end over a fake GcpClient (billing's query monkeypatched)."""

from datetime import date
from decimal import Decimal

import pytest

from ai_cost_tracker.connectors.gcp import GcpConnector, billing, ids
from ai_cost_tracker.core.models import CostRecord
from tests.connectors.test_gcp_inventory import (
    EP_URL,
    SEARCH_URL,
    endpoints_route,
    search_route,
    service_url,
)
from tests.connectors.test_gcp_monitoring import (
    END,
    PROJECT,
    PROJECT_URL,
    SERIES_URL,
    START,
    FakeGcpClient,
    load,
    series_route,
)


def client() -> FakeGcpClient:
    return FakeGcpClient(
        {
            SEARCH_URL: search_route,
            service_url("demo-ai"): load("inventory/service_enabled.json"),
            service_url("web-shop"): load("inventory/service_disabled.json"),
            service_url("data-lake"): 403,
            PROJECT_URL: load("monitoring/project.json"),
            SERIES_URL: series_route,
            EP_URL: endpoints_route,
        }
    )


def connector(**kwargs) -> GcpConnector:
    kwargs.setdefault("regions", ["us-central1"])
    return GcpConnector(client(), **kwargs)


async def test_identity():
    conn = connector()
    assert (conn.name, conn.cloud) == ("gcp", "gcp")


async def test_list_accounts_filters_and_discovery():
    assert [a.account_id for a in await connector().list_accounts()] == ["demo-ai"]
    everything = await connector().list_accounts(include_all=True)
    assert [a.account_id for a in everything] == ["data-lake", "demo-ai", "web-shop"]
    configured = await connector(project_ids=["Demo-AI"]).list_accounts()
    assert [(a.account_id, a.name) for a in configured] == [("demo-ai", "Demo AI")]


async def test_check_access():
    conn = connector()
    assert await conn.check_access(PROJECT) is True
    assert await conn.check_access("someone-else") is False


async def test_deployments_and_usage_join_on_resource_id():
    conn = connector()
    deps = await conn.list_deployments(PROJECT)
    usage = await conn.pull_usage(PROJECT, deps, START, END)
    known = {(d.resource_id, d.name) for d in deps}
    assert usage and {(u.resource_id, u.deployment) for u in usage} <= known
    assert {u.cloud for u in usage} == {"gcp"}
    assert sum(u.input_tokens for u in usage) == 1200 + 800 + 5000 + 40
    assert sum(u.output_tokens for u in usage) == 150 + 90 + 700
    assert sum(u.requests for u in usage) == 6 + 4 + 3 + 1
    assert await conn.pull_ptu_utilization(PROJECT, deps, START, END) == []


async def test_pull_cost_delegates_to_billing(monkeypatch: pytest.MonkeyPatch):
    seen = {}
    record = CostRecord(
        cloud="gcp",
        sub_account_id=PROJECT,
        resource_id=ids.resource_id(PROJECT, "us-central1"),
        charge_period_start=date(2025, 3, 8),
        service_name="Vertex AI",
        sku_meter="Gemini 2.5 Flash Text Input - Predictions",
        billed_cost=Decimal("1.25"),
        billing_currency="EUR",
    )

    async def fake_pull_cost(client, **kwargs):
        seen.update(kwargs, client=client)
        return [record]

    monkeypatch.setattr(billing, "pull_cost", fake_pull_cost)
    conn = connector(
        billing_table="billing-proj.export.gcp_billing_export_v1", billing_location="EU"
    )
    assert await conn.pull_cost(PROJECT, date(2025, 3, 1), date(2025, 3, 8)) == [record]
    assert seen["table"] == "billing-proj.export.gcp_billing_export_v1"
    assert (seen["location"], seen["project_id"]) == ("EU", PROJECT)
    assert (seen["start"], seen["end"]) == (date(2025, 3, 1), date(2025, 3, 8))


async def test_pull_cost_without_table_is_a_clear_error():
    with pytest.raises(RuntimeError, match="ACT_GCP_BILLING_EXPORT_TABLE"):
        await connector().pull_cost(PROJECT, date(2025, 3, 1), date(2025, 3, 8))


async def test_aclose_closes_client():
    conn = connector()
    await conn.aclose()
    assert conn._client.closed
