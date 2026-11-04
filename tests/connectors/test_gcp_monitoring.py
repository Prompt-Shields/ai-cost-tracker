"""Cloud Monitoring token metrics -> hourly TokenUsage, over a fake GcpClient and JSON fixtures
shaped like the public ``projects.timeSeries.list`` REST docs."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_cost_tracker.connectors.gcp import ids, monitoring
from ai_cost_tracker.core.models import Deployment

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gcp"
PROJECT = "demo-ai"
SERIES_URL = f"{monitoring.MONITORING}/projects/{PROJECT}/timeSeries"
PROJECT_URL = f"{monitoring.RESOURCE_MANAGER}/projects/{PROJECT}"
START = datetime(2025, 3, 8, 20, tzinfo=UTC)
END = datetime(2025, 3, 9, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def http_error(status: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


Route = dict | int | Callable[[dict[str, Any]], dict | int]


class FakeGcpClient:
    """GcpClient stand-in: absolute URL -> payload, status code to raise, or callable(params)."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._answer("GET", url, params)

    async def post(
        self, url: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._answer("POST", url, params)

    async def aclose(self) -> None:
        self.closed = True

    def _answer(self, method: str, url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((method, url, params))
        route = self.routes.get(url, 404)
        result = route(params) if callable(route) else route
        if isinstance(result, int):
            raise http_error(result, url)
        return copy.deepcopy(result)

    def urls(self, url: str) -> list[dict[str, Any]]:
        return [p for _, u, p in self.calls if u == url]


def series_route(params: dict[str, Any]) -> dict | int:
    """Token pages 1-2 by pageToken, invocations by filter, headers view for inventory."""
    if params.get("view") == "HEADERS":
        return load("monitoring/token_count_headers.json")
    if monitoring.INVOCATION_METRIC in params["filter"]:
        return load("monitoring/invocation_count.json")
    if params.get("pageToken") == "page-2":
        return load("monitoring/token_count_page2.json")
    return load("monitoring/token_count_page1.json")


def monitoring_client(**extra: Route) -> FakeGcpClient:
    return FakeGcpClient(
        {PROJECT_URL: load("monitoring/project.json"), SERIES_URL: series_route, **extra}
    )


def rid(location: str) -> str:
    return ids.resource_id(PROJECT, location)


async def test_pull_usage_hourly_per_model_with_pagination():
    client = monitoring_client()
    rows = await monitoring.pull_usage(client, PROJECT, [], START, END)

    got = {
        (r.resource_id, r.deployment, r.bucket_start.hour): (
            r.input_tokens,
            r.output_tokens,
            r.requests,
        )
        for r in rows
    }
    assert got == {
        (rid("us-central1"), "gemini-2.5-flash", 20): (1200, 150, 6),
        (rid("us-central1"), "gemini-2.5-flash", 21): (800, 90, 4),
        (rid("global"), "gemini-2.5-pro", 22): (5000, 700, 3),
        (rid("europe-west4"), "gemini-2.0-flash-001", 23): (40, 0, 1),
    }
    for r in rows:
        assert r.cloud == "gcp" and r.account_id == PROJECT and r.source == "metrics"
        assert r.bucket_start.tzinfo is not None and r.bucket_start.utcoffset() == timedelta(0)
    # Every model seen is returned, with a canonical model name even without inventory.
    assert {r.deployment: r.model for r in rows}["gemini-2.0-flash-001"] == "gemini-2.0-flash"
    # Token metric fetched across both pages, the invocation metric once.
    token_calls = [p for p in client.urls(SERIES_URL) if monitoring.TOKEN_METRIC in p["filter"]]
    assert [p.get("pageToken") for p in token_calls] == [None, "page-2"]


async def test_pull_usage_drops_series_from_other_projects_in_metrics_scope():
    rows = await monitoring.pull_usage(monitoring_client(), PROJECT, [], START, END)
    flash = [r for r in rows if r.deployment == "gemini-2.5-flash" and r.bucket_start.hour == 21]
    assert flash[0].input_tokens == 800  # not + 999999 from container 999999999999


async def test_pull_usage_keeps_all_series_when_project_unreadable():
    rows = await monitoring.pull_usage(
        monitoring_client(**{PROJECT_URL: 403}), PROJECT, [], START, END
    )
    flash = [r for r in rows if r.deployment == "gemini-2.5-flash" and r.bucket_start.hour == 21]
    assert flash[0].input_tokens == 800 + 999999


async def test_pull_usage_query_params():
    client = monitoring_client()
    await monitoring.pull_usage(client, PROJECT, [], START, END)
    tokens, invocations = client.urls(SERIES_URL)[0], client.urls(SERIES_URL)[-1]
    assert tokens["filter"] == (
        f'metric.type = "{monitoring.TOKEN_METRIC}" '
        f'AND resource.type = "aiplatform.googleapis.com/PublisherModel"'
    )
    assert tokens["interval.startTime"] == "2025-03-08T20:00:00Z"
    assert tokens["interval.endTime"] == "2025-03-09T00:00:00Z"
    assert tokens["aggregation.alignmentPeriod"] == "3600s"
    assert tokens["aggregation.perSeriesAligner"] == "ALIGN_SUM"
    assert tokens["aggregation.crossSeriesReducer"] == "REDUCE_SUM"
    assert set(tokens["aggregation.groupByFields"]) == {
        "resource.labels.resource_container",
        "resource.labels.location",
        "resource.labels.model_user_id",
        "metric.labels.type",
    }
    assert monitoring.INVOCATION_METRIC in invocations["filter"]
    assert "metric.labels.type" not in invocations["aggregation.groupByFields"]


async def test_pull_usage_uses_inventory_model_names():
    dep = Deployment(
        cloud="gcp",
        account_id=PROJECT,
        resource_id=rid("global"),
        name="gemini-2.5-pro",
        model="gemini-2.5-pro-custom-name",
    )
    rows = await monitoring.pull_usage(monitoring_client(), PROJECT, [dep], START, END)
    assert {r.model for r in rows if r.deployment == "gemini-2.5-pro"} == {
        "gemini-2.5-pro-custom-name"
    }


async def test_pull_usage_chunks_to_seven_days_on_whole_hours_without_double_counting():
    # Each chunk answers with one point ending exactly at its end time.
    def route(params: dict[str, Any]) -> dict:
        end = params["interval.endTime"]
        start = monitoring.rfc3339(monitoring.parse_time(end) - timedelta(hours=1))
        series = {
            "metric": {"type": monitoring.TOKEN_METRIC, "labels": {"type": "input"}},
            "resource": {
                "type": monitoring.RESOURCE_TYPE,
                "labels": {"location": "us-central1", "model_user_id": "gemini-2.5-flash"},
            },
            "points": [
                {"interval": {"startTime": start, "endTime": end}, "value": {"int64Value": "5"}},
                # The point ending at the chunk start belongs to the previous chunk.
                {
                    "interval": {
                        "startTime": params["interval.startTime"],
                        "endTime": params["interval.startTime"],
                    },
                    "value": {"int64Value": "7"},
                },
            ],
        }
        return {"timeSeries": [series]} if monitoring.TOKEN_METRIC in params["filter"] else {}

    client = monitoring_client(**{SERIES_URL: route})
    start = datetime(2025, 3, 1, 10, 25, tzinfo=UTC)
    end = datetime(2025, 3, 16, 9, 5, tzinfo=UTC)
    rows = await monitoring.pull_usage(client, PROJECT, [], start, end)

    token_calls = [p for p in client.urls(SERIES_URL) if monitoring.TOKEN_METRIC in p["filter"]]
    spans = [(p["interval.startTime"], p["interval.endTime"]) for p in token_calls]
    assert spans == [
        ("2025-03-01T10:00:00Z", "2025-03-08T10:00:00Z"),
        ("2025-03-08T10:00:00Z", "2025-03-15T10:00:00Z"),
        ("2025-03-15T10:00:00Z", "2025-03-16T10:00:00Z"),
    ]
    assert [(r.bucket_start, r.input_tokens) for r in rows] == [
        (datetime(2025, 3, 8, 9, tzinfo=UTC), 5),
        (datetime(2025, 3, 15, 9, tzinfo=UTC), 5),
        (datetime(2025, 3, 16, 9, tzinfo=UTC), 5),
    ]


async def test_pull_usage_is_deterministic():
    a = await monitoring.pull_usage(monitoring_client(), PROJECT, [], START, END)
    b = await monitoring.pull_usage(monitoring_client(), PROJECT, [], START, END)
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]


async def test_pull_usage_raises_on_monitoring_403():
    client = monitoring_client(**{SERIES_URL: 403})
    with pytest.raises(httpx.HTTPStatusError):
        await monitoring.pull_usage(client, PROJECT, [], START, END)


async def test_seen_models_uses_headers_view_and_filters_containers():
    client = monitoring_client()
    seen = await monitoring.seen_models(client, PROJECT, START, END)
    assert seen == {
        ("us-central1", "gemini-2.5-flash"),
        ("europe-west4", "gemini-2.0-flash-001"),
        ("global", "gemini-2.5-pro"),
    }
    assert client.urls(SERIES_URL)[0]["view"] == "HEADERS"


def test_point_value_and_time_parsing():
    assert monitoring.point_value({"value": {"int64Value": "12"}}) == 12
    assert monitoring.point_value({"value": {"doubleValue": 3.6}}) == 4
    assert monitoring.point_value({}) == 0
    assert monitoring.parse_time("2025-03-08T21:00:00.123456789Z") == datetime(
        2025, 3, 8, 21, 0, 0, 123456, tzinfo=UTC
    )
    point = {"interval": {"endTime": "2025-03-08T22:00:00Z"}}
    assert monitoring.point_bucket(point) == datetime(2025, 3, 8, 21, tzinfo=UTC)
