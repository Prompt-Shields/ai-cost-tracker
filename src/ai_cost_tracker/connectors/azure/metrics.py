"""Azure Monitor token metrics for Cognitive Services accounts.

Metric names differ between classic Azure OpenAI and newer AI Foundry resources, so the names
are configurable here; the phase 0 spike lists ``metricDefinitions`` per resource to confirm
them (ARCHITECTURE.md §14).
"""

from datetime import datetime
from typing import Any

from ai_cost_tracker.core.models import Deployment, TokenUsage

API_VERSION = "2023-10-01"

INPUT_METRIC = "ProcessedPromptTokens"
OUTPUT_METRIC = "GeneratedTokens"
REQUESTS_METRIC = "AzureOpenAIRequests"
DEPLOYMENT_DIMENSION = "ModelDeploymentName"


def metrics_url(resource_id: str, start: datetime, end: datetime) -> str:
    names = ",".join([INPUT_METRIC, OUTPUT_METRIC, REQUESTS_METRIC])
    timespan = f"{_iso(start)}/{_iso(end)}"
    return (
        f"{resource_id}/providers/Microsoft.Insights/metrics?api-version={API_VERSION}"
        f"&metricnames={names}&timespan={timespan}&interval=PT1H&aggregation=Total"
        f"&$filter={DEPLOYMENT_DIMENSION} eq '*'"
    )


def parse_metrics(
    account_id: str, resource_id: str, deployments: list[Deployment], payload: dict[str, Any]
) -> list[TokenUsage]:
    models = {d.name: d.model for d in deployments}
    buckets: dict[tuple[str, str], TokenUsage] = {}
    field_for = {
        INPUT_METRIC: "input_tokens",
        OUTPUT_METRIC: "output_tokens",
        REQUESTS_METRIC: "requests",
    }

    for metric in payload.get("value", []):
        field = field_for.get(metric.get("name", {}).get("value", ""))
        if field is None:
            continue
        for series in metric.get("timeseries", []):
            deployment = _dimension(series, DEPLOYMENT_DIMENSION)
            if not deployment:
                continue
            for point in series.get("data", []):
                total = point.get("total")
                if not total:
                    continue
                stamp = point["timeStamp"]
                key = (deployment, stamp)
                usage = buckets.get(key)
                if usage is None:
                    usage = buckets[key] = TokenUsage(
                        cloud="azure",
                        account_id=account_id,
                        resource_id=resource_id,
                        deployment=deployment,
                        model=models.get(deployment),
                        bucket_start=datetime.fromisoformat(stamp.replace("Z", "+00:00")),
                    )
                setattr(usage, field, getattr(usage, field) + int(total))
    return list(buckets.values())


def _dimension(series: dict[str, Any], name: str) -> str | None:
    for item in series.get("metadatavalues", []):
        if item.get("name", {}).get("value", "").lower() == name.lower():
            return item.get("value")
    return None


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
