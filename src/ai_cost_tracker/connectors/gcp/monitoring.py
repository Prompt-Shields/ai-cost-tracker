"""Cloud Monitoring token metrics for Vertex AI publisher models -> hourly ``TokenUsage``.

Request and response shapes follow Google's public REST docs for ``projects.timeSeries.list``
(Cloud Monitoring API v3). Not yet seen on a live project:

- Assumption (spike-gcp): metric types ``aiplatform.googleapis.com/publisher/online_serving/
  token_count`` (DELTA INT64, label ``type`` = ``input`` / ``output``) and ``.../
  model_invocation_count`` (requests), on monitored resource type
  ``aiplatform.googleapis.com/PublisherModel`` with resource labels ``resource_container``,
  ``location``, ``publisher``, ``model_user_id`` and ``model_version_id``;
- Assumption (spike-gcp): ``model_user_id`` is the model id as callers name it
  (``gemini-2.5-flash``, ``gemini-2.0-flash-001``), which is also the deployment name;
- Assumption (spike-gcp): ``resource_container`` is the project number (or ``projects/<n>``)
  for the project that served the call. A project that is a metrics-scope host also returns
  its monitored projects' series, so series from other containers are dropped;
- Assumption (spike-gcp): with ``alignmentPeriod=3600s`` the aligned periods end at
  ``interval.endTime`` and step back by an hour, so whole-hour intervals give whole UTC hours,
  and an ``endTime`` up to an hour in the future is accepted (it lets the current, partial
  hour show up within minutes instead of after the hour ends).
"""

import logging
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ai_cost_tracker.connectors.gcp import ids
from ai_cost_tracker.core.models import Deployment, TokenUsage
from ai_cost_tracker.normalise import gcp_skus

log = logging.getLogger(__name__)

MONITORING = "https://monitoring.googleapis.com/v3"
RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v3"

TOKEN_METRIC = "aiplatform.googleapis.com/publisher/online_serving/token_count"
INVOCATION_METRIC = "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"
RESOURCE_TYPE = "aiplatform.googleapis.com/PublisherModel"

LOCATION = "resource.labels.location"
MODEL = "resource.labels.model_user_id"
CONTAINER = "resource.labels.resource_container"
TOKEN_TYPE = "metric.labels.type"

HOUR = timedelta(hours=1)
MAX_SPAN = timedelta(days=7)
PAGE_SIZE = 1000

# Assumption (spike-gcp): values of the ``type`` label on token_count.
_FIELD_FOR_TYPE = {"input": "input_tokens", "output": "output_tokens"}


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

    Start is floored and end ceiled to the hour so every aligned period is one UTC hour.
    """
    lo, hi = floor_hour(start), ceil_hour(end)
    chunks = []
    while lo < hi:
        stop = min(lo + MAX_SPAN, hi)
        chunks.append((lo, stop))
        lo = stop
    return chunks


def metric_filter(metric: str) -> str:
    return f'metric.type = "{metric}" AND resource.type = "{RESOURCE_TYPE}"'


def series_params(metric: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Query params for hourly sums per location and model (and token type)."""
    group_by = [CONTAINER, LOCATION, MODEL]
    if metric == TOKEN_METRIC:
        group_by.append(TOKEN_TYPE)
    return {
        "filter": metric_filter(metric),
        "interval.startTime": rfc3339(start),
        "interval.endTime": rfc3339(end),
        "aggregation.alignmentPeriod": "3600s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": group_by,
        "pageSize": PAGE_SIZE,
    }


async def iter_series(client: Any, project_id: str, params: dict[str, Any]) -> AsyncIterator[dict]:
    """Every time series across ``nextPageToken`` pages."""
    url = f"{MONITORING}/projects/{project_id}/timeSeries"
    token: str | None = None
    while True:
        page = await client.get(url, params={**params, "pageToken": token} if token else params)
        for series in page.get("timeSeries") or []:
            yield series
        token = page.get("nextPageToken")
        if not token:
            return


async def project_containers(client: Any, project_id: str) -> set[str] | None:
    """Values ``resource_container`` may take for this project; None if unknown (keep all)."""
    try:
        project = await client.get(f"{RESOURCE_MANAGER}/projects/{project_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (403, 404):
            log.warning("cannot read project %s; not filtering series by project", project_id)
            return None
        raise
    name = project.get("name") or ""  # "projects/<number>"
    values = {project_id.lower(), name.lower()}
    if name.startswith("projects/"):
        values.add(name.removeprefix("projects/"))
    return {v for v in values if v}


def labels(series: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """(container, location, model, token type) of one series."""
    resource = (series.get("resource") or {}).get("labels") or {}
    metric = (series.get("metric") or {}).get("labels") or {}
    return (
        resource.get("resource_container"),
        resource.get("location"),
        resource.get("model_user_id"),
        metric.get("type"),
    )


def point_value(point: dict[str, Any]) -> int:
    """INT64 values arrive as JSON strings; a DOUBLE after reduction is rounded."""
    value = point.get("value") or {}
    if "int64Value" in value:
        return int(value["int64Value"])
    if "doubleValue" in value:
        return round(float(value["doubleValue"]))
    return 0


def point_bucket(point: dict[str, Any]) -> datetime | None:
    """UTC hour the aligned point covers: the hour ending at ``interval.endTime``."""
    end = (point.get("interval") or {}).get("endTime")
    if not end:
        return None
    return floor_hour(parse_time(end) - timedelta(microseconds=1))


def parse_series(
    project_id: str,
    field: str | None,
    series_list: Iterable[dict[str, Any]],
    *,
    models: dict[tuple[str, str], str],
    containers: set[str] | None,
    window: tuple[datetime, datetime],
    buckets: dict[tuple[str, str, datetime], TokenUsage],
) -> None:
    """Add each point to ``buckets`` keyed by (resource_id, deployment, hour).

    ``field`` None means token_count: the series' ``type`` label picks input or output.
    Points outside ``window`` (the chunk) are dropped, so adjacent chunks never double count.
    """
    lo, hi = window
    for series in series_list:
        container, location, model, token_type = labels(series)
        if not model:
            continue
        if containers is not None and container and container.lower() not in containers:
            continue
        target = field or _FIELD_FOR_TYPE.get((token_type or "").lower())
        if target is None:
            continue
        rid = ids.resource_id(project_id, location)
        for point in series.get("points") or []:
            bucket = point_bucket(point)
            amount = point_value(point)
            if bucket is None or not lo <= bucket < hi or amount <= 0:
                continue
            key = (rid, model, bucket)
            usage = buckets.get(key)
            if usage is None:
                usage = buckets[key] = TokenUsage(
                    cloud="gcp",
                    account_id=project_id,
                    resource_id=rid,
                    deployment=model,
                    model=models.get((rid, model)) or canonical(model),
                    bucket_start=bucket,
                )
            setattr(usage, target, getattr(usage, target) + amount)


async def pull_usage(
    client: Any, project_id: str, deployments: list[Deployment], start: datetime, end: datetime
) -> list[TokenUsage]:
    """Hourly tokens and requests for every publisher model the project served.

    Not limited to ``deployments``: ingestion replaces the whole account window, and a model
    first called since the last inventory sync must still count. ``deployments`` only supply
    the canonical model name where known.
    """
    models = {(d.resource_id, d.name): d.model for d in deployments}
    containers = await project_containers(client, project_id)
    buckets: dict[tuple[str, str, datetime], TokenUsage] = {}
    for lo, hi in time_chunks(start, end):
        for metric, field in ((TOKEN_METRIC, None), (INVOCATION_METRIC, "requests")):
            params = series_params(metric, lo, hi)
            series = [s async for s in iter_series(client, project_id, params)]
            parse_series(
                project_id,
                field,
                series,
                models=models,
                containers=containers,
                window=(lo, hi),
                buckets=buckets,
            )
    return sorted(buckets.values(), key=lambda u: (u.resource_id, u.deployment, u.bucket_start))


async def seen_models(
    client: Any, project_id: str, start: datetime, end: datetime
) -> set[tuple[str, str]]:
    """(location, model_user_id) of every publisher model with token series in the window.

    Uses ``view=HEADERS`` so no points come back; one call (plus pages) whatever the span.
    """
    params = {
        "filter": metric_filter(TOKEN_METRIC),
        "interval.startTime": rfc3339(start),
        "interval.endTime": rfc3339(end),
        "view": "HEADERS",
        "pageSize": PAGE_SIZE,
    }
    containers = await project_containers(client, project_id)
    seen: set[tuple[str, str]] = set()
    async for series in iter_series(client, project_id, params):
        container, location, model, _ = labels(series)
        if not model:
            continue
        if containers is not None and container and container.lower() not in containers:
            continue
        seen.add(((location or ids.GLOBAL).lower(), model))
    return seen


def canonical(name: str) -> str:
    return gcp_skus.canonical_model(name) or name


def parse_time(value: str) -> datetime:
    """RFC3339 with ``Z`` and up to nanosecond fractions (Python takes microseconds)."""
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, rest = text.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(digits) :]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tz}"
    return to_utc(datetime.fromisoformat(text))


def rfc3339(value: datetime) -> str:
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
