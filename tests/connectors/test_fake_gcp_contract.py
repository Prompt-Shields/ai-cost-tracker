import pytest

from ai_cost_tracker.connectors.fake_gcp import FakeGcpConnector
from tests.connectors.contract import ConnectorContract


class TestFakeGcpConnectorContract(ConnectorContract):
    @pytest.fixture
    def connector(self) -> FakeGcpConnector:
        return FakeGcpConnector()
