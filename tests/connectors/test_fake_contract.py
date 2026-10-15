import pytest

from ai_cost_tracker.connectors.fake import FakeConnector
from tests.connectors.contract import ConnectorContract


class TestFakeConnectorContract(ConnectorContract):
    @pytest.fixture
    def connector(self) -> FakeConnector:
        return FakeConnector()
