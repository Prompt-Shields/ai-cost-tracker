"""GCP projects and Vertex AI inventory over a fake GcpClient and REST-doc-shaped fixtures."""

from typing import Any

import httpx
import pytest

from ai_cost_tracker.connectors.gcp import ids, inventory, monitoring
from tests.connectors.test_gcp_monitoring import (
    PROJECT,
    PROJECT_URL,
    SERIES_URL,
    FakeGcpClient,
    load,
    series_route,
)

SEARCH_URL = f"{inventory.RESOURCE_MANAGER}/projects:search"
EP_URL = inventory.endpoints_url(PROJECT, "us-central1")


def service_url(project: str) -> str:
    return f"{inventory.SERVICE_USAGE}/projects/{project}/services/aiplatform.googleapis.com"


def search_route(params: dict[str, Any]) -> dict:
    assert params["query"] == "state:ACTIVE"
    if params.get("pageToken") == "next-1":
        return load("inventory/search_page2.json")
    return load("inventory/search_page1.json")


def endpoints_route(params: dict[str, Any]) -> dict:
    if params.get("pageToken") == "ep-2":
        return load("inventory/endpoints_us_central1_page2.json")
    return load("inventory/endpoints_us_central1_page1.json")


def projects_client(**extra: Any) -> FakeGcpClient:
    return FakeGcpClient(
        {
            SEARCH_URL: search_route,
            service_url("demo-ai"): load("inventory/service_enabled.json"),
            service_url("web-shop"): load("inventory/service_disabled.json"),
            service_url("data-lake"): 403,
            PROJECT_URL: load("monitoring/project.json"),
            **extra,
        }
    )


async def test_list_projects_default_returns_vertex_users_only():
    client = projects_client()
    accounts = await inventory.list_projects(client, include_all=False, only=set())
    assert [(a.account_id, a.name, a.has_ai_resources) for a in accounts] == [
        ("demo-ai", "Demo AI", True)
    ]
    assert all(a.cloud == "gcp" for a in accounts)
    assert [p.get("pageToken") for p in client.urls(SEARCH_URL)] == [None, "next-1"]


async def test_list_projects_include_all_paginates_and_skips_inactive():
    accounts = await inventory.list_projects(projects_client(), include_all=True, only=set())
    assert [(a.account_id, a.has_ai_resources) for a in accounts] == [
        ("data-lake", False),  # Service Usage 403 -> not flagged, not an error
        ("demo-ai", True),
        ("web-shop", False),
    ]


async def test_list_projects_only_fetches_each_and_skips_invisible():
    client = projects_client(
        **{f"{inventory.RESOURCE_MANAGER}/projects/hidden-proj": 403},
    )
    accounts = await inventory.list_projects(
        client, include_all=False, only={"demo-ai", "hidden-proj", "gone-proj"}
    )
    assert [(a.account_id, a.has_ai_resources) for a in accounts] == [("demo-ai", True)]
    assert client.urls(SEARCH_URL) == []


async def test_list_projects_raises_on_server_error():
    client = projects_client(**{service_url("demo-ai"): 500})
    with pytest.raises(httpx.HTTPStatusError):
        await inventory.list_projects(client, include_all=True, only=set())


@pytest.mark.parametrize(("route", "expected"), [(None, True), (403, False), (404, False)])
async def test_check_access(route, expected):
    client = projects_client(**({PROJECT_URL: route} if route else {}))
    assert await inventory.check_access(client, PROJECT) is expected


async def test_check_access_raises_other_errors():
    with pytest.raises(httpx.HTTPStatusError):
        await inventory.check_access(projects_client(**{PROJECT_URL: 500}), PROJECT)


def test_endpoints_url_regional_and_global_hosts():
    assert inventory.endpoints_url("p", "europe-west4") == (
        "https://europe-west4-aiplatform.googleapis.com/v1/projects/p/locations/europe-west4/endpoints"
    )
    assert inventory.endpoints_url("p", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints"
    )


def inventory_client(**extra: Any) -> FakeGcpClient:
    return FakeGcpClient(
        {
            PROJECT_URL: load("monitoring/project.json"),
            SERIES_URL: series_route,
            EP_URL: endpoints_route,
            inventory.endpoints_url(PROJECT, "europe-west4"): 403,
            **extra,
        }
    )


async def test_list_deployments_publisher_models_and_endpoints():
    client = inventory_client()
    deps = await inventory.list_deployments(
        client, PROJECT, ["us-central1", "europe-west4", "US-CENTRAL1"]
    )
    by_name = {(d.resource_id, d.name): d for d in deps}
    assert set(by_name) == {
        (ids.resource_id(PROJECT, "us-central1"), "gemini-2.5-flash"),
        (ids.resource_id(PROJECT, "europe-west4"), "gemini-2.0-flash-001"),
        (ids.resource_id(PROJECT, "global"), "gemini-2.5-pro"),
        (ids.resource_id(PROJECT, "us-central1"), "endpoints/1111"),
        (ids.resource_id(PROJECT, "us-central1"), "endpoints/3333"),
    }
    old = by_name[(ids.resource_id(PROJECT, "europe-west4"), "gemini-2.0-flash-001")]
    assert (old.model, old.model_version, old.sku, old.region) == (
        "gemini-2.0-flash",
        "001",
        None,
        "europe-west4",
    )
    assert all(d.cloud == "gcp" and d.account_id == PROJECT for d in deps)

    ep = by_name[(ids.resource_id(PROJECT, "us-central1"), "endpoints/1111")]
    # The deployed model with 90% of traffic names the endpoint's model.
    assert ep.model == monitoring.canonical("llama-3-8b-instruct")
    assert (ep.sku, ep.model_version, ep.region) == ("dedicated", "3", "us-central1")
    assert ep.tags == {"team": "search", "env": "prod"}

    # Publisher models come from the last 30 days of headers; endpoints paginate; the
    # duplicate region is asked once.
    headers = client.urls(SERIES_URL)[0]
    span = monitoring.parse_time(headers["interval.endTime"]) - monitoring.parse_time(
        headers["interval.startTime"]
    )
    assert (headers["view"], span.days) == ("HEADERS", 30)
    assert [p.get("pageToken") for p in client.urls(EP_URL)] == [None, "ep-2"]


async def test_list_deployments_without_monitoring_access_still_lists_endpoints():
    client = inventory_client(**{SERIES_URL: 403})
    deps = await inventory.list_deployments(client, PROJECT, ["us-central1"])
    assert sorted(d.name for d in deps) == ["endpoints/1111", "endpoints/3333"]


async def test_list_deployments_raises_on_endpoint_server_error():
    client = inventory_client(**{EP_URL: 500})
    with pytest.raises(httpx.HTTPStatusError):
        await inventory.list_deployments(client, PROJECT, ["us-central1"])
