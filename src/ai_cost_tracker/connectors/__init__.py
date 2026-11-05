import logging

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.base import Connector

log = logging.getLogger(__name__)


def get_connector(settings: Settings) -> Connector:
    """The primary (Azure or fake Azure) connector."""
    if settings.connector == "azure":
        from ai_cost_tracker.connectors.azure import AzureConnector

        return AzureConnector(subscription_ids=settings.azure_subscription_ids)

    from ai_cost_tracker.connectors.fake import FakeConnector

    return FakeConnector()


def get_connectors(settings: Settings) -> list[Connector]:
    """One connector per cloud the worker should ingest, primary first.

    The fake setup always adds the demo GCP project and AWS account so the multi-cloud views
    have data; real GCP / AWS connectors join only when ``gcp_enabled`` / ``aws_enabled``.
    """
    connectors = [get_connector(settings)]
    if settings.connector == "fake":
        from ai_cost_tracker.connectors.fake_aws import FakeAwsConnector
        from ai_cost_tracker.connectors.fake_gcp import FakeGcpConnector

        connectors += [FakeGcpConnector(), FakeAwsConnector()]
        return connectors
    if settings.gcp_enabled:
        from ai_cost_tracker.connectors.gcp import GcpConnector

        connectors.append(GcpConnector.from_settings(settings))
    if settings.aws_enabled:
        from ai_cost_tracker.connectors.aws import AwsConnector

        connectors.append(AwsConnector.from_settings(settings))
    return connectors


def get_connector_for(settings: Settings, cloud: str) -> Connector:
    """The connector that reads ``cloud``; LookupError when that cloud isn't enabled."""
    for connector in get_connectors(settings):
        if connector.cloud == cloud:
            return connector
    raise LookupError(f"no connector enabled for cloud {cloud!r}")


__all__ = ["Connector", "get_connector", "get_connector_for", "get_connectors"]
