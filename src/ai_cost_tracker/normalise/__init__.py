"""Per-cloud meter parsing behind one entry point, so ingestion and estimates stay cloud-blind.

``parse_meter(cloud, meter)`` turns a billing meter (Azure meter name, GCP SKU description,
AWS usage type) into a ``MeterInfo``; ``canonical_model(cloud, name)`` maps a deployment's
or metric's model name onto the same model names, so cost, tokens and prices join.
"""

from ai_cost_tracker.normalise import aws_usage, azure_meters, gcp_skus
from ai_cost_tracker.normalise.azure_meters import MeterInfo


def parse_meter(cloud: str, meter: str) -> MeterInfo | None:
    """Parsed meter for ``cloud``; None when unmapped or the cloud has no parser yet."""
    if cloud == "gcp":
        return gcp_skus.parse_sku(meter)
    if cloud == "aws":
        return aws_usage.parse_usage_type(meter)
    if cloud == "azure":
        return azure_meters.parse_meter(meter)
    return None


def canonical_model(cloud: str, name: str | None) -> str | None:
    """Canonical model name shared by deployments, token metrics, cost lines and prices."""
    if not name:
        return None
    if cloud == "gcp":
        return gcp_skus.canonical_model(name)
    if cloud == "aws":
        return aws_usage.canonical_model(name)
    info = azure_meters.parse_meter(name)
    return info.model if info else name.strip().lower()


__all__ = ["MeterInfo", "canonical_model", "parse_meter"]
