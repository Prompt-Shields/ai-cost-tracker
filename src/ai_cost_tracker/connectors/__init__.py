from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.base import Connector


def get_connector(settings: Settings) -> Connector:
    if settings.connector == "azure":
        from ai_cost_tracker.connectors.azure import AzureConnector

        return AzureConnector(subscription_ids=settings.azure_subscription_ids)

    from ai_cost_tracker.connectors.fake import FakeConnector

    return FakeConnector()


__all__ = ["Connector", "get_connector"]
