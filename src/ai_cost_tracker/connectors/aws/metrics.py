"""CloudWatch ``AWS/Bedrock`` token metrics -> hourly ``TokenUsage`` (issue #54).

Mirrors ``connectors/gcp/monitoring.py``: whole-hour UTC buckets, time chunks that never
double count at their edges, every model seen returned (not only known deployments) and the
current, partial hour included so tokens show up within minutes. Request and response shapes
follow AWS's public CloudWatch API reference (``ListMetrics``, ``GetMetricData``) and the
Bedrock "Monitor with CloudWatch" docs. Not yet seen on a live account:

- Assumption (spike-aws): Bedrock publishes ``InputTokenCount``, ``OutputTokenCount`` and
  ``Invocations`` in namespace ``AWS/Bedrock`` with the single dimension ``ModelId``, whose
  value is the model id or inference profile id as the caller passed it
  (``anthropic.claude-3-5-haiku-20241022-v1:0``, ``us.anthropic.claude-3-7-sonnet-...``).
- Assumption (spike-aws): prompt caching adds ``CacheReadInputTokenCount`` and
  ``CacheWriteInputTokenCount``, which are *not* included in ``InputTokenCount``. We add them
  to ``input_tokens`` so the token total matches what was processed. TokenUsage has no cache
  split, so estimates price cached tokens at the full input rate; billed cost is unaffected.
- Assumption (spike-aws): with ``Period=3600`` and hour-aligned ``StartTime``/``EndTime`` the
  returned ``Timestamps`` are the UTC hour each sum starts at, and an ``EndTime`` in the
  future is accepted (the current hour's sum so far comes back).
- Assumption (spike-aws): ``ListMetrics`` only returns metrics with data points in the last
  two weeks (per its docs), so a backfill further back also queries the known deployments of
  the region by name.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_cost_tracker.connectors.aws import ids
from ai_cost_tracker.connectors.aws.auth import AwsAccessDenied
from ai_cost_tracker.core.models import Deployment, TokenUsage
from ai_cost_tracker.normalise import aws_usage

log = logging.getLogger(__name__)

NAMESPACE = "AWS/Bedrock"
DIMENSION = "ModelId"
PERIOD = 3600
HOUR = timedelta(hours=1)

# GetMetricData takes ≤ 500 queries and returns ≤ 100,800 data points per call
# (CloudWatch quotas page). 500 queries × 168 hourly points = 84,000, so a 7-day chunk with
# a full batch stays under the point limit; NextToken pagination covers the rest.
MAX_QUERIES = 500
MAX_SPAN = timedelta(days=7)

# Metric name -> TokenUsage field. Cache metrics count as input (see module docstring).
FIELD_FOR_METRIC = {
    "InputTokenCount": "input_tokens",
    "CacheReadInputTokenCount": "input_tokens",
    "CacheWriteInputTokenCount": "input_tokens",
    "OutputTokenCount": "output_tokens",
    "Invocations": "requests",
}
TOKEN_METRICS = frozenset(
    {"InputTokenCount", "OutputTokenCount", "CacheReadInputTokenCount", "CacheWriteInputTokenCount"}
)

# Assumption (spike-aws): error codes a region answers with when it isn't enabled for the
# account (opt-in regions) or the service isn't offered there; skipped like a denied region.
UNAVAILABLE_CODES = frozenset(
    {"OptInRequired", "InvalidClientTokenId", "UnrecognizedClientException", "EndpointError"}
)


@dataclass(frozen=True)
class Query:
    id: str
    model_id: str
    metric: str


def to_utc(value: datetime) -> datetime:
    """Naive datetimes are treated as UTC, not local time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def floor_hour(value: datetime) -> datetime:
    return to_utc(value).replace(minute=0, second=0, microsecond=0)


def ceil_hour(value: datetime) -> datetime:
    floored = floor_hour(value)
    return floored if floored == to_utc(value) else floored + HOUR


def time_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Whole-hour pieces of ≤ ``MAX_SPAN`` covering ``[start, end)``.

    CloudWatch recommends period-aligned start/end times; flooring the start and ceiling the
    end makes every returned sum exactly one UTC hour (the last one possibly still running).
    """
    lo, hi = floor_hour(start), ceil_hour(end)
    chunks = []
    while lo < hi:
        stop = min(lo + MAX_SPAN, hi)
        chunks.append((lo, stop))
        lo = stop
    return chunks


def model_dimension(metric: dict[str, Any]) -> str | None:
    """The ``ModelId`` of a ListMetrics entry whose only dimension is ``ModelId``.

    Entries with extra dimensions would be a different aggregation of the same calls, so
    they're ignored rather than summed twice.
    """
    dims = metric.get("Dimensions") or []
    if len(dims) != 1 or dims[0].get("Name") != DIMENSION:
        return None
    return dims[0].get("Value") or None


async def list_model_metrics(sessions: Any, client: Any) -> set[tuple[str, str]]:
    """(ModelId, metric name) pairs CloudWatch lists for Bedrock in this client's region."""
    metrics = await sessions.paginate(
        client, "list_metrics", "Metrics", Namespace=NAMESPACE, Dimensions=[{"Name": DIMENSION}]
    )
    pairs = set()
    for metric in metrics:
        name, model = metric.get("MetricName"), model_dimension(metric)
        if model and name in FIELD_FOR_METRIC:
            pairs.add((model, name))
    return pairs


def build_queries(pairs: Iterable[tuple[str, str]]) -> list[Query]:
    """One Sum query per pair, with ids CloudWatch accepts (``^[a-z][a-zA-Z0-9_]*$``)."""
    return [Query(f"q{i}", model, metric) for i, (model, metric) in enumerate(sorted(pairs))]


def metric_data_queries(batch: list[Query]) -> list[dict[str, Any]]:
    return [
        {
            "Id": q.id,
            "MetricStat": {
                "Metric": {
                    "Namespace": NAMESPACE,
                    "MetricName": q.metric,
                    "Dimensions": [{"Name": DIMENSION, "Value": q.model_id}],
                },
                "Period": PERIOD,
                "Stat": "Sum",
            },
            "ReturnData": True,
        }
        for q in batch
    ]


def parse_timestamp(value: Any) -> datetime:
    """boto3 returns aware datetimes; fixtures and raw JSON carry ISO strings."""
    if isinstance(value, datetime):
        return to_utc(value)
    return to_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def collect(
    results: Iterable[dict[str, Any]],
    queries: dict[str, Query],
    window: tuple[datetime, datetime],
    points: dict[tuple[str, str, datetime], int],
) -> None:
    """Record each (model, metric, hour) sum in ``points``.

    Keyed rather than added so a data point repeated across result pages counts once, and
    points outside ``window`` (the chunk) are dropped so adjacent chunks never double count.
    """
    lo, hi = window
    for result in results:
        query = queries.get(result.get("Id", ""))
        if query is None:
            continue
        if result.get("StatusCode") == "InternalError":
            log.warning("CloudWatch InternalError for %s %s", query.model_id, query.metric)
        for stamp, value in zip(
            result.get("Timestamps") or [], result.get("Values") or [], strict=False
        ):
            bucket = floor_hour(parse_timestamp(stamp))
            amount = round(float(value))
            if lo <= bucket < hi and amount > 0:
                points[(query.model_id, query.metric, bucket)] = amount


async def region_points(
    sessions: Any, client: Any, queries: list[Query], start: datetime, end: datetime
) -> dict[tuple[str, str, datetime], int]:
    """Hourly sums for ``queries``, ≤ ``MAX_QUERIES`` per call, per time chunk."""
    points: dict[tuple[str, str, datetime], int] = {}
    by_id = {q.id: q for q in queries}
    for lo, hi in time_chunks(start, end):
        for i in range(0, len(queries), MAX_QUERIES):
            batch = queries[i : i + MAX_QUERIES]
            results = await sessions.paginate(
                client,
                "get_metric_data",
                "MetricDataResults",
                MetricDataQueries=metric_data_queries(batch),
                StartTime=lo,
                EndTime=hi,
                ScanBy="TimestampAscending",
            )
            collect(results, by_id, (lo, hi), points)
    return points


def error_code(exc: Exception) -> str:
    """botocore ClientError code, read duck-typed so this module needs no botocore import."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def region_of(deployment: Deployment) -> str | None:
    try:
        return ids.parse_resource_id(deployment.resource_id)[1]
    except ValueError:
        return (deployment.region or "").lower() or None


def known_pairs(
    deployments: list[Deployment], account_id: str, region: str
) -> set[tuple[str, str]]:
    """Every metric for on-demand deployments of this region, in case ListMetrics forgot them.

    Provisioned throughputs are left out: their deployment name isn't a CloudWatch ModelId.
    """
    pairs = set()
    for d in deployments:
        if d.account_id != account_id or d.sku == "ProvisionedThroughput":
            continue
        if region_of(d) == region.lower():
            pairs.update((d.name, metric) for metric in FIELD_FOR_METRIC)
    return pairs


async def pull_usage(
    sessions: Any,
    account_id: str,
    regions: list[str],
    deployments: list[Deployment],
    start: datetime,
    end: datetime,
) -> list[TokenUsage]:
    """Hourly tokens and requests for every Bedrock model the account used, per region.

    Not limited to ``deployments``: ingestion replaces the whole account window, and a model
    first called since the last inventory sync must still count. ``deployments`` supply the
    canonical model name where known and names to query beyond ListMetrics' two weeks.

    A region that is denied or unavailable is skipped with a log line; if every region was
    skipped and at least one was denied, AwsAccessDenied is raised so ingestion reports it.
    """
    models = {(d.resource_id, d.name): d.model for d in deployments}
    buckets: dict[tuple[str, str, datetime], TokenUsage] = {}
    denied: list[str] = []
    skipped = 0
    for region in regions:
        rid = ids.resource_id(account_id, region)
        try:
            client = await sessions.client("cloudwatch", account_id, region)
            pairs = await list_model_metrics(sessions, client)
            pairs |= known_pairs(deployments, account_id, region)
            points = await region_points(sessions, client, build_queries(pairs), start, end)
        except AwsAccessDenied as exc:
            log.warning("CloudWatch denied for account %s in %s: %s", account_id, region, exc)
            denied.append(region)
            skipped += 1
            continue
        except Exception as exc:
            if error_code(exc) not in UNAVAILABLE_CODES:
                raise
            log.warning("CloudWatch unavailable for %s in %s: %s", account_id, region, exc)
            skipped += 1
            continue
        for (model_id, metric, bucket), amount in points.items():
            key = (rid, model_id, bucket)
            usage = buckets.get(key)
            if usage is None:
                usage = buckets[key] = TokenUsage(
                    cloud="aws",
                    account_id=account_id,
                    resource_id=rid,
                    deployment=model_id,
                    model=models.get((rid, model_id)) or canonical(model_id),
                    bucket_start=bucket,
                )
            field = FIELD_FOR_METRIC[metric]
            setattr(usage, field, getattr(usage, field) + amount)
    if regions and denied and skipped == len(regions):
        raise AwsAccessDenied(
            f"CloudWatch access denied for account {account_id} in {', '.join(denied)}"
        )
    return sorted(buckets.values(), key=lambda u: (u.resource_id, u.deployment, u.bucket_start))


async def seen_model_ids(sessions: Any, account_id: str, region: str, days: int = 30) -> list[str]:
    """ModelId values with Bedrock token metrics in ``region``, for W9's inventory.

    One paginated ListMetrics call, no data points. ListMetrics can't be narrowed by age
    beyond "last 3 hours", so ``days`` is honoured only as far as its own window goes
    (about two weeks: Assumption (spike-aws)). AwsAccessDenied propagates; the caller skips
    the region.
    """
    del days  # kept for the contract; see docstring
    client = await sessions.client("cloudwatch", account_id, region)
    pairs = await list_model_metrics(sessions, client)
    return sorted({model for model, metric in pairs if metric in TOKEN_METRICS})


def canonical(model_id: str) -> str:
    return aws_usage.canonical_model(model_id) or model_id
