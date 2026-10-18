"""Azure Monitor token metrics for Cognitive Services accounts.

Unverified until the phase 0 spike (issue #2) lists ``metricDefinitions`` on a live resource
(ARCHITECTURE.md §14):
- metric names. Classic Azure OpenAI exposes ``ProcessedPromptTokens`` / ``GeneratedTokens``
  / ``AzureOpenAIRequests``; newer AI Foundry resources may use different names, so they are
  module constants;
- the ``ModelDeploymentName`` dimension name (matched case-insensitively);
- the per-request timespan limit. We send at most seven days per call to stay well inside
  documented limits and keep responses small;
- the ``top`` cap. Splitting by a dimension returns only the top 10 series unless ``top`` is
  raised, which would silently drop deployments on busy accounts;
- the PTU utilisation metric (issue #32): name ``AzureOpenAIProvisionedManagedUtilizationV2``,
  unit percent (0-100), split by ``ModelDeploymentName``, with ``Average`` and ``Maximum``
  aggregations; and whether Monitor reports idle hours as 0 or leaves them out (we skip
  points without an average, so a left-out hour counts as "no data", not as idle).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.core.models import Deployment, TokenUsage

API_VERSION = "2023-10-01"

INPUT_METRIC = "ProcessedPromptTokens"
OUTPUT_METRIC = "GeneratedTokens"
REQUESTS_METRIC = "AzureOpenAIRequests"
DEPLOYMENT_DIMENSION = "ModelDeploymentName"
# Unverified (#2): provisioned-managed utilisation in percent, per deployment.
PTU_UTILIZATION_METRIC = "AzureOpenAIProvisionedManagedUtilizationV2"

MAX_SPAN = timedelta(days=7)
MAX_SERIES = 1000

_FIELD_FOR = {
    INPUT_METRIC.lower(): "input_tokens",
    OUTPUT_METRIC.lower(): "output_tokens",
    REQUESTS_METRIC.lower(): "requests",
}


def to_utc(value: datetime) -> datetime:
    """Naive datetimes are treated as UTC, not local time."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def time_chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` into ≤ ``MAX_SPAN`` pieces, start floored to the hour.

    Flooring keeps hourly buckets aligned to clock hours (assumption: Monitor aligns
    ``PT1H`` buckets to the timespan start).
    """
    start = to_utc(start).replace(minute=0, second=0, microsecond=0)
    end = to_utc(end)
    chunks = []
    while start < end:
        stop = min(start + MAX_SPAN, end)
        chunks.append((start, stop))
        start = stop
    return chunks


def metrics_request(resource_id: str, start: datetime, end: datetime) -> tuple[str, dict]:
    """Path and query params for one metrics call; httpx URL-encodes ``$filter`` etc."""
    return (
        f"{resource_id}/providers/Microsoft.Insights/metrics",
        {
            "api-version": API_VERSION,
            "metricnames": ",".join([INPUT_METRIC, OUTPUT_METRIC, REQUESTS_METRIC]),
            "timespan": f"{_iso(start)}/{_iso(end)}",
            "interval": "PT1H",
            "aggregation": "Total",
            "$filter": f"{DEPLOYMENT_DIMENSION} eq '*'",
            "top": str(MAX_SERIES),
        },
    )


def parse_metrics(
    account_id: str, resource_id: str, deployments: list[Deployment], payload: dict[str, Any]
) -> list[TokenUsage]:
    """One ``TokenUsage`` per (deployment, hour) with any non-zero metric.

    Deployments seen in metrics but missing from ``deployments`` (created since the last
    inventory sync) are kept with ``model=None`` rather than dropped.
    """
    rid = resource_id.lower()
    models = {d.name: d.model for d in deployments if d.resource_id.lower() == rid}
    buckets: dict[tuple[str, datetime], TokenUsage] = {}

    for metric in payload.get("value") or []:
        field = _FIELD_FOR.get(((metric.get("name") or {}).get("value") or "").lower())
        if field is None:
            continue
        for series in metric.get("timeseries") or []:
            deployment = _dimension(series, DEPLOYMENT_DIMENSION)
            if not deployment:
                continue
            for point in series.get("data") or []:
                total = point.get("total")
                if not total:
                    continue
                stamp = to_utc(datetime.fromisoformat(point["timeStamp"].replace("Z", "+00:00")))
                usage = buckets.get((deployment, stamp))
                if usage is None:
                    usage = buckets[(deployment, stamp)] = TokenUsage(
                        cloud="azure",
                        account_id=account_id,
                        resource_id=resource_id,
                        deployment=deployment,
                        model=models.get(deployment),
                        bucket_start=stamp,
                    )
                setattr(usage, field, getattr(usage, field) + round(total))
    return sorted(buckets.values(), key=lambda u: (u.deployment, u.bucket_start))


def ptu_request(resource_id: str, start: datetime, end: datetime) -> tuple[str, dict]:
    """Path and query params for one hourly PTU utilisation call (average and peak)."""
    return (
        f"{resource_id}/providers/Microsoft.Insights/metrics",
        {
            "api-version": API_VERSION,
            "metricnames": PTU_UTILIZATION_METRIC,
            "timespan": f"{_iso(start)}/{_iso(end)}",
            "interval": "PT1H",
            "aggregation": "Average,Maximum",
            "$filter": f"{DEPLOYMENT_DIMENSION} eq '*'",
            "top": str(MAX_SERIES),
        },
    )


def parse_ptu(resource_id: str, wanted: set[str], payload: dict[str, Any]) -> list[PtuSample]:
    """One ``PtuSample`` per (deployment in ``wanted``, hour) that has an average.

    Deployment names are matched case-insensitively and reported as spelled in ``wanted``,
    so samples join the inventory even if Monitor changes the case. Percent becomes a 0..1
    fraction, clamped: Monitor can report slightly over 100% when a deployment bursts.
    """
    names = {w.lower(): w for w in wanted}
    samples: dict[tuple[str, datetime], PtuSample] = {}
    for metric in payload.get("value") or []:
        name = ((metric.get("name") or {}).get("value") or "").lower()
        if name != PTU_UTILIZATION_METRIC.lower():
            continue
        for series in metric.get("timeseries") or []:
            deployment = names.get((_dimension(series, DEPLOYMENT_DIMENSION) or "").lower())
            if deployment is None:
                continue
            for point in series.get("data") or []:
                avg = point.get("average")
                if avg is None:
                    continue
                stamp = to_utc(datetime.fromisoformat(point["timeStamp"].replace("Z", "+00:00")))
                peak = point.get("maximum")
                samples[(deployment, stamp)] = PtuSample(
                    resource_id=resource_id,
                    deployment=deployment,
                    bucket_start=stamp,
                    utilization=_fraction(avg),
                    max_utilization=_fraction(peak) if peak is not None else None,
                )
    return sorted(samples.values(), key=lambda s: (s.deployment, s.bucket_start))


def _fraction(percent: float | int) -> Decimal:
    value = Decimal(str(percent)) / 100
    return min(Decimal(1), max(Decimal(0), value)).quantize(Decimal("0.0001"))


def _dimension(series: dict[str, Any], name: str) -> str | None:
    for item in series.get("metadatavalues") or []:
        if ((item.get("name") or {}).get("value") or "").lower() == name.lower():
            return item.get("value")
    return None


def _iso(value: datetime) -> str:
    return to_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
