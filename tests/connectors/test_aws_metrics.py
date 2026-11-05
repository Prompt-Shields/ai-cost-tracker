"""CloudWatch AWS/Bedrock metrics -> hourly TokenUsage, over a fake ``AwsSessions`` answering
from JSON fixtures shaped like the public ListMetrics / GetMetricData API reference."""

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ai_cost_tracker.connectors.aws import ids, metrics
from ai_cost_tracker.connectors.aws.auth import AwsAccessDenied
from ai_cost_tracker.core.models import Deployment

FIXTURES = Path(__file__).parent.parent / "fixtures" / "aws" / "metrics"
ACCOUNT = "123456789012"
START = datetime(2025, 3, 8, 20, tzinfo=UTC)
END = datetime(2025, 3, 9, 0, tzinfo=UTC)
HAIKU = "anthropic.claude-3-5-haiku-20241022-v1:0"
SONNET = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
NOVA = "eu.amazon.nova-pro-v1:0"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class ClientError(Exception):
    """Shaped like botocore's ClientError (``.response["Error"]["Code"]``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeClient:
    def __init__(self, service: str, account_id: str, region: str) -> None:
        self.service, self.account_id, self.region = service, account_id, region


class FakeSessions:
    """``AwsSessions`` stand-in: canned ListMetrics per region, GetMetricData from datapoints.

    ``fail`` maps a region to the exception its client raises. Every GetMetricData answer
    repeats its first data point as an extra result, as a second page could.
    """

    def __init__(
        self,
        *,
        fail: dict[str, Exception] | None = None,
        listings: dict[str, dict] | None = None,
        points: dict[str, Any] | None = None,
    ) -> None:
        self.fail = fail or {}
        self.listings = listings
        self.points = points if points is not None else load("datapoints.json")
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    async def client(self, service: str, account_id: str, region: str) -> FakeClient:
        assert service == "cloudwatch"
        if region in self.fail:
            raise self.fail[region]
        return FakeClient(service, account_id, region)

    async def call(self, client: FakeClient, method: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"unexpected call {method}")

    async def paginate(
        self, client: FakeClient, method: str, result_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((client.region, method, kwargs))
        if method == "list_metrics":
            assert result_key == "Metrics"
            return copy.deepcopy(self.listing(client.region)["Metrics"])
        assert method == "get_metric_data" and result_key == "MetricDataResults"
        return self.metric_data(client.region, **kwargs)

    async def aclose(self) -> None:
        self.closed = True

    def listing(self, region: str) -> dict:
        if self.listings is not None:
            return self.listings.get(region, {"Metrics": []})
        path = FIXTURES / f"list_metrics_{region}.json"
        return load(path.name) if path.exists() else {"Metrics": []}

    def metric_data(
        self, region: str, *, MetricDataQueries, StartTime, EndTime, **_: Any
    ) -> list[dict[str, Any]]:
        results = []
        for q in MetricDataQueries:
            metric = q["MetricStat"]["Metric"]
            model = metric["Dimensions"][0]["Value"]
            series = self.points.get(region, {}).get(model, {}).get(metric["MetricName"], {})
            stamps = sorted(
                (datetime.fromisoformat(t.replace("Z", "+00:00")), v)
                for t, v in series.items()
                if StartTime <= datetime.fromisoformat(t.replace("Z", "+00:00")) < EndTime
            )
            results.append(
                {
                    "Id": q["Id"],
                    "Label": metric["MetricName"],
                    "Timestamps": [t for t, _ in stamps],
                    "Values": [float(v) for _, v in stamps],
                    "StatusCode": "Complete",
                }
            )
        dupes = [
            {**r, "Timestamps": r["Timestamps"][:1], "Values": r["Values"][:1]}
            for r in results
            if r["Timestamps"]
        ]
        return results + dupes

    def data_calls(self) -> list[dict[str, Any]]:
        return [kw for _, m, kw in self.calls if m == "get_metric_data"]


def rid(region: str) -> str:
    return ids.resource_id(ACCOUNT, region)


async def test_pull_usage_hourly_per_model_across_regions():
    rows = await metrics.pull_usage(
        FakeSessions(), ACCOUNT, ["us-east-1", "eu-west-1"], [], START, END
    )
    got = {
        (r.resource_id, r.deployment, r.bucket_start.hour): (
            r.input_tokens,
            r.output_tokens,
            r.requests,
        )
        for r in rows
    }
    assert got == {
        (rid("us-east-1"), HAIKU, 20): (1200, 150, 6),
        (rid("us-east-1"), HAIKU, 21): (800, 90, 4),
        # Cache read + write tokens are added to input; points outside [START, END) dropped.
        (rid("us-east-1"), SONNET, 22): (5000 + 2000 + 300, 700, 3),
        (rid("eu-west-1"), NOVA, 23): (40, 0, 1),
    }
    for r in rows:
        assert r.cloud == "aws" and r.account_id == ACCOUNT and r.source == "metrics"
        assert r.bucket_start.utcoffset() == timedelta(0)
    names = {r.deployment: r.model for r in rows}
    assert names[HAIKU] == metrics.aws_usage.canonical_model(HAIKU)
    assert names[SONNET] == metrics.aws_usage.canonical_model(SONNET)
    assert rows == sorted(rows, key=lambda u: (u.resource_id, u.deployment, u.bucket_start))


async def test_pull_usage_queries_listed_pairs_only_with_sum_hourly():
    sessions = FakeSessions()
    await metrics.pull_usage(sessions, ACCOUNT, ["us-east-1"], [], START, END)
    listing = [kw for _, m, kw in sessions.calls if m == "list_metrics"]
    assert listing == [{"Namespace": "AWS/Bedrock", "Dimensions": [{"Name": "ModelId"}]}]
    (call,) = sessions.data_calls()
    assert (call["StartTime"], call["EndTime"]) == (START, END)
    queried = set()
    for q in call["MetricDataQueries"]:
        stat = q["MetricStat"]
        assert (stat["Period"], stat["Stat"], q["ReturnData"]) == (3600, "Sum", True)
        assert stat["Metric"]["Namespace"] == "AWS/Bedrock"
        assert q["Id"][0].islower() and q["Id"].isalnum()
        queried.add((stat["Metric"]["Dimensions"][0]["Value"], stat["Metric"]["MetricName"]))
    # InvocationLatency, dimensionless and multi-dimension entries are not queried.
    assert queried == {
        (HAIKU, "InputTokenCount"),
        (HAIKU, "OutputTokenCount"),
        (HAIKU, "Invocations"),
        (SONNET, "InputTokenCount"),
        (SONNET, "OutputTokenCount"),
        (SONNET, "Invocations"),
        (SONNET, "CacheReadInputTokenCount"),
        (SONNET, "CacheWriteInputTokenCount"),
        ("amazon.titan-embed-text-v2:0", "Invocations"),
    }
    assert len({q["Id"] for q in call["MetricDataQueries"]}) == len(queried)


async def test_pull_usage_batches_at_most_500_queries_per_call():
    models = [f"amazon.model-{i}-v1:0" for i in range(200)]
    listing = {
        "Metrics": [
            {"MetricName": m, "Dimensions": [{"Name": "ModelId", "Value": model}]}
            for model in models
            for m in ("InputTokenCount", "OutputTokenCount", "Invocations")
        ]
    }
    points = {
        "us-east-1": {model: {"InputTokenCount": {"2025-03-08T21:00:00Z": 1}} for model in models}
    }
    sessions = FakeSessions(listings={"us-east-1": listing}, points=points)
    rows = await metrics.pull_usage(sessions, ACCOUNT, ["us-east-1"], [], START, END)
    sizes = [len(c["MetricDataQueries"]) for c in sessions.data_calls()]
    assert sizes == [500, 100]
    assert len(rows) == 200 and sum(r.input_tokens for r in rows) == 200


async def test_pull_usage_chunks_to_seven_days_on_whole_hours_without_double_counting():
    start = datetime(2025, 3, 1, 10, 25, tzinfo=UTC)
    end = datetime(2025, 3, 16, 9, 5, tzinfo=UTC)
    # Points exactly on both chunk boundaries and the partial last hour.
    hours = ["2025-03-01T10:00:00Z", "2025-03-08T10:00:00Z", "2025-03-15T10:00:00Z"]
    hours.append("2025-03-16T09:00:00Z")
    points = {"us-east-1": {HAIKU: {"InputTokenCount": {h: 5 for h in hours}}}}
    sessions = FakeSessions(points=points)
    rows = await metrics.pull_usage(sessions, ACCOUNT, ["us-east-1"], [], start, end)

    windows = [(c["StartTime"], c["EndTime"]) for c in sessions.data_calls()]
    assert windows == [
        (datetime(2025, 3, 1, 10, tzinfo=UTC), datetime(2025, 3, 8, 10, tzinfo=UTC)),
        (datetime(2025, 3, 8, 10, tzinfo=UTC), datetime(2025, 3, 15, 10, tzinfo=UTC)),
        (datetime(2025, 3, 15, 10, tzinfo=UTC), datetime(2025, 3, 16, 10, tzinfo=UTC)),
    ]
    assert all(hi - lo <= metrics.MAX_SPAN for lo, hi in windows)
    # A full batch over a full chunk stays under GetMetricData's 100,800 data point limit.
    assert metrics.MAX_QUERIES * (metrics.MAX_SPAN // metrics.HOUR) <= 100_800
    assert [(r.bucket_start.day, r.bucket_start.hour, r.input_tokens) for r in rows] == [
        (1, 10, 5),
        (8, 10, 5),
        (15, 10, 5),
        (16, 9, 5),
    ]


async def test_pull_usage_treats_naive_datetimes_as_utc():
    rows = await metrics.pull_usage(
        FakeSessions(), ACCOUNT, ["us-east-1"], [], START.replace(tzinfo=None), END
    )
    assert {r.bucket_start.hour for r in rows} == {20, 21, 22}


async def test_pull_usage_uses_inventory_names_and_queries_known_deployments():
    # A deployment ListMetrics doesn't list (older than two weeks) is still queried by name.
    old = "anthropic.claude-3-haiku-20240307-v1:0"
    points = load("datapoints.json")
    points["us-east-1"][old] = {"OutputTokenCount": {"2025-03-08T20:00:00Z": 11}}
    deps = [
        Deployment(
            cloud="aws",
            account_id=ACCOUNT,
            resource_id=rid("us-east-1"),
            name=HAIKU,
            model="haiku-from-inventory",
        ),
        Deployment(
            cloud="aws", account_id=ACCOUNT, resource_id=rid("us-east-1"), name=old, model="x"
        ),
        Deployment(
            cloud="aws",
            account_id=ACCOUNT,
            resource_id=rid("us-east-1"),
            name="my-provisioned",
            model="claude-3-5-haiku",
            sku="ProvisionedThroughput",
        ),
        Deployment(
            cloud="aws", account_id=ACCOUNT, resource_id=rid("eu-west-1"), name="eu-only", model="y"
        ),
    ]
    sessions = FakeSessions(points=points)
    rows = await metrics.pull_usage(sessions, ACCOUNT, ["us-east-1"], deps, START, END)
    assert {r.model for r in rows if r.deployment == HAIKU} == {"haiku-from-inventory"}
    assert [(r.deployment, r.output_tokens, r.model) for r in rows if r.deployment == old] == [
        (old, 11, "x")
    ]
    queried = {
        q["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        for q in sessions.data_calls()[0]["MetricDataQueries"]
    }
    assert old in queried and "my-provisioned" not in queried and "eu-only" not in queried


async def test_pull_usage_skips_denied_and_unavailable_regions():
    sessions = FakeSessions(
        fail={"ap-south-1": AwsAccessDenied("denied"), "me-south-1": ClientError("OptInRequired")}
    )
    rows = await metrics.pull_usage(
        sessions, ACCOUNT, ["ap-south-1", "us-east-1", "me-south-1"], [], START, END
    )
    assert {r.resource_id for r in rows} == {rid("us-east-1")}


async def test_pull_usage_raises_when_every_region_denied():
    fail = {"us-east-1": AwsAccessDenied("no"), "eu-west-1": ClientError("OptInRequired")}
    with pytest.raises(AwsAccessDenied, match="us-east-1"):
        await metrics.pull_usage(
            FakeSessions(fail=fail), ACCOUNT, ["us-east-1", "eu-west-1"], [], START, END
        )


async def test_pull_usage_reraises_other_errors():
    sessions = FakeSessions(fail={"us-east-1": ClientError("ThrottlingException")})
    with pytest.raises(ClientError):
        await metrics.pull_usage(sessions, ACCOUNT, ["us-east-1"], [], START, END)


async def test_pull_usage_all_regions_unavailable_returns_nothing():
    sessions = FakeSessions(fail={"me-south-1": ClientError("OptInRequired")})
    assert await metrics.pull_usage(sessions, ACCOUNT, ["me-south-1"], [], START, END) == []


async def test_pull_usage_is_deterministic():
    one = await metrics.pull_usage(FakeSessions(), ACCOUNT, ["us-east-1"], [], START, END)
    two = await metrics.pull_usage(FakeSessions(), ACCOUNT, ["us-east-1"], [], START, END)
    assert one == two


async def test_seen_model_ids_lists_models_with_token_metrics():
    sessions = FakeSessions()
    assert await metrics.seen_model_ids(sessions, ACCOUNT, "us-east-1") == [HAIKU, SONNET]
    assert await metrics.seen_model_ids(sessions, ACCOUNT, "eu-west-1", days=7) == [NOVA]
    assert await metrics.seen_model_ids(sessions, ACCOUNT, "sa-east-1") == []
    assert sessions.data_calls() == []


async def test_seen_model_ids_propagates_denied():
    with pytest.raises(AwsAccessDenied):
        await metrics.seen_model_ids(
            FakeSessions(fail={"us-east-1": AwsAccessDenied("no")}), ACCOUNT, "us-east-1"
        )
