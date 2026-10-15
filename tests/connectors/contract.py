"""Reusable behavioural contract for ``Connector`` implementations.

Subclass ``ConnectorContract`` in a ``Test*`` class and provide a ``connector`` fixture; every
connector (fake, Azure with a mocked transport, later GCP/AWS) must pass the same checks so
ingestion can treat them interchangeably. Override the date-range class attributes if a
connector's recorded fixtures only cover particular days.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from ai_cost_tracker.connectors.base import Connector
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage


def _canonical(records: list) -> list[str]:
    """Order-insensitive form, so determinism checks don't depend on API paging order."""
    return sorted(r.model_dump_json() for r in records)


class ConnectorContract:
    cost_start: date = date(2025, 3, 3)
    cost_end: date = date(2025, 3, 9)
    usage_start: datetime = datetime(2025, 3, 8, 20, tzinfo=UTC)
    usage_end: datetime = datetime(2025, 3, 9, 4, tzinfo=UTC)

    @pytest.fixture
    def connector(self) -> Connector:
        raise NotImplementedError("subclasses must provide a `connector` fixture")

    @pytest.fixture
    async def account_id(self, connector: Connector) -> str:
        accounts = await connector.list_accounts()
        assert accounts, "contract needs at least one account"
        return accounts[0].account_id

    @pytest.fixture
    async def deployments(self, connector: Connector, account_id: str) -> list[Deployment]:
        deployments = await connector.list_deployments(account_id)
        assert deployments, "contract needs at least one deployment"
        return deployments

    async def test_name(self, connector: Connector) -> None:
        assert isinstance(connector.name, str) and connector.name

    async def test_list_accounts(self, connector: Connector) -> None:
        accounts = await connector.list_accounts()
        assert accounts
        for account in accounts:
            assert isinstance(account, CloudAccount)
            assert account.account_id.strip()
        ids = [a.account_id for a in accounts]
        assert len(ids) == len(set(ids)), "account ids must be unique"

    async def test_list_deployments(
        self, connector: Connector, account_id: str, deployments: list[Deployment]
    ) -> None:
        for dep in deployments:
            assert isinstance(dep, Deployment)
            assert dep.account_id == account_id
            assert dep.model.strip()
            assert dep.resource_id.strip()
            assert dep.name.strip()

    async def test_pull_cost(self, connector: Connector, account_id: str) -> None:
        records = await connector.pull_cost(account_id, self.cost_start, self.cost_end)
        assert records
        days = set()
        for rec in records:
            assert isinstance(rec, CostRecord)
            assert self.cost_start <= rec.charge_period_start <= self.cost_end
            assert isinstance(rec.billed_cost, Decimal)
            assert rec.billed_cost >= 0
            if rec.consumed_quantity is not None:
                assert isinstance(rec.consumed_quantity, Decimal)
                assert rec.consumed_quantity >= 0
            assert len(rec.billing_currency) == 3 and rec.billing_currency.isalpha()
            assert rec.billing_currency.isupper()
            assert rec.sku_meter.strip()
            days.add(rec.charge_period_start)
        # End is inclusive: a connector that treats it as exclusive drops the last day.
        assert self.cost_end in days

    async def test_pull_cost_single_day(self, connector: Connector, account_id: str) -> None:
        records = await connector.pull_cost(account_id, self.cost_end, self.cost_end)
        assert {r.charge_period_start for r in records} <= {self.cost_end}

    async def test_pull_usage(
        self, connector: Connector, account_id: str, deployments: list[Deployment]
    ) -> None:
        rows = await connector.pull_usage(account_id, deployments, self.usage_start, self.usage_end)
        assert rows
        known = {(d.resource_id, d.name) for d in deployments}
        for row in rows:
            assert isinstance(row, TokenUsage)
            assert row.account_id == account_id
            assert (row.resource_id, row.deployment) in known
            b = row.bucket_start
            assert b.tzinfo is not None and b.utcoffset() == timedelta(0)
            assert (b.minute, b.second, b.microsecond) == (0, 0, 0), "buckets are hourly"
            assert self.usage_start <= b < self.usage_end
            for n in (row.input_tokens, row.output_tokens, row.requests):
                assert isinstance(n, int) and n >= 0
        keys = [(r.resource_id, r.deployment, r.bucket_start) for r in rows]
        assert len(keys) == len(set(keys)), "one row per deployment and hour"

    async def test_pull_usage_only_requested_deployments(
        self, connector: Connector, account_id: str, deployments: list[Deployment]
    ) -> None:
        wanted = deployments[:1]
        rows = await connector.pull_usage(account_id, wanted, self.usage_start, self.usage_end)
        assert {(r.resource_id, r.deployment) for r in rows} <= {
            (d.resource_id, d.name) for d in wanted
        }

    async def test_deterministic(
        self, connector: Connector, account_id: str, deployments: list[Deployment]
    ) -> None:
        """Idempotent ingestion replaces a window with a re-pull, so re-pulls must agree."""
        assert _canonical(await connector.list_accounts()) == _canonical(
            await connector.list_accounts()
        )
        assert _canonical(await connector.list_deployments(account_id)) == _canonical(
            await connector.list_deployments(account_id)
        )
        cost = [await connector.pull_cost(account_id, self.cost_start, self.cost_end)]
        cost.append(await connector.pull_cost(account_id, self.cost_start, self.cost_end))
        assert _canonical(cost[0]) == _canonical(cost[1])
        args = (account_id, deployments, self.usage_start, self.usage_end)
        assert _canonical(await connector.pull_usage(*args)) == _canonical(
            await connector.pull_usage(*args)
        )
