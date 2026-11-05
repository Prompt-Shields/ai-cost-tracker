import pytest

from ai_cost_tracker.connectors.fake_aws import FakeAwsConnector
from tests.connectors.contract import ConnectorContract


class TestFakeAwsConnectorContract(ConnectorContract):
    @pytest.fixture
    def connector(self) -> FakeAwsConnector:
        return FakeAwsConnector()
