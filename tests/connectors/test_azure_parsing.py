"""Pure helpers: Query API body/rows, month chunking, metrics request and parsing."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ai_cost_tracker.connectors.azure import cost_query, metrics
from ai_cost_tracker.core.models import Deployment

FIXTURES = Path(__file__).parent.parent / "fixtures" / "azure"
SUB = "11111111-1111-1111-1111-111111111111"
EASTUS = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-eastus"
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parse_rows_cost_column_numeric_date():
    rows = cost_query.parse_rows(SUB, load("cost_page1.json"))
    assert len(rows) == 2
    first = rows[0]
    assert first.charge_period_start == date(2026, 10, 12)
    assert first.billed_cost == Decimal("1.234567")
    assert first.consumed_quantity == Decimal("0.4938268")
    assert first.sku_meter == "gpt-4o-0806 Inp glbl 1M Tokens"
    assert first.billing_currency == "USD"
    assert first.resource_id.endswith("/accounts/aoai-eastus")


def test_parse_rows_pretax_column_string_date_any_order():
    rows = cost_query.parse_rows("33333333-3333-3333-3333-333333333333", load("cost_payg.json"))
    assert [r.charge_period_start for r in rows] == [date(2026, 9, 30), date(2026, 10, 1)]
    assert rows[0].billed_cost == Decimal("0.75")
    assert rows[0].billing_currency == "EUR"
    assert rows[1].resource_id is None  # empty ResourceId for unassigned cost
    assert rows[1].consumed_quantity is None


def test_parse_rows_float_usage_date():
    payload = {
        "properties": {
            "columns": [{"name": "PreTaxCost"}, {"name": "UsageDate"}, {"name": "Meter"}],
            "rows": [[2, 20261001.0, "m"]],
        }
    }
    assert cost_query.parse_rows(SUB, payload)[0].charge_period_start == date(2026, 10, 1)


def test_parse_rows_rejects_unknown_columns():
    with pytest.raises(ValueError, match="unexpected"):
        cost_query.parse_rows(SUB, {"properties": {"columns": [{"name": "Foo"}], "rows": []}})


def test_next_link():
    assert cost_query.next_link(load("cost_page1.json")).startswith("https://management.azure")
    assert cost_query.next_link(load("cost_page2.json")) is None


def test_month_chunks():
    assert cost_query.month_chunks(date(2026, 1, 15), date(2026, 3, 2)) == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 2)),
    ]
    assert cost_query.month_chunks(date(2026, 10, 5), date(2026, 10, 5)) == [
        (date(2026, 10, 5), date(2026, 10, 5))
    ]
    assert cost_query.month_chunks(date(2026, 10, 5), date(2026, 10, 4)) == []
    assert len(cost_query.month_chunks(date(2025, 10, 15), date(2026, 10, 14))) == 13


def test_build_query_cost_column():
    body = cost_query.build_query(date(2026, 10, 1), date(2026, 10, 31), "PreTaxCost")
    assert body["timePeriod"] == {"from": "2026-10-01T00:00:00Z", "to": "2026-10-31T23:59:59Z"}
    assert body["dataset"]["aggregation"]["totalCost"]["name"] == "PreTaxCost"
    assert len(body["dataset"]["grouping"]) == 2


def test_time_chunks_seven_days_and_hour_floor():
    start = datetime(2026, 9, 1, 10, 30, tzinfo=UTC)
    end = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
    chunks = metrics.time_chunks(start, end)
    assert chunks[0][0] == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert chunks[-1][1] == end
    assert all(b - a <= timedelta(days=7) for a, b in chunks)
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))


def test_time_chunks_naive_is_utc():
    chunks = metrics.time_chunks(datetime(2026, 9, 1), datetime(2026, 9, 1, 2))
    assert chunks == [(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1, 2, tzinfo=UTC))]


def test_metrics_request_params():
    path, params = metrics.metrics_request(
        EASTUS, datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC)
    )
    assert path == f"{EASTUS}/providers/Microsoft.Insights/metrics"
    assert params["$filter"] == "ModelDeploymentName eq '*'"
    assert params["timespan"] == "2026-10-12T00:00:00Z/2026-10-13T00:00:00Z"
    assert params["interval"] == "PT1H"
    assert int(params["top"]) > 10


def _deployment(name: str, model: str, resource_id: str = EASTUS) -> Deployment:
    return Deployment(
        cloud="azure", account_id=SUB, resource_id=resource_id, name=name, model=model
    )


def test_parse_metrics_multiple_deployments_and_metrics():
    deployments = [
        _deployment("search-chat-gpt4o", "gpt-4o"),
        _deployment("search-embed", "text-embedding-3-small"),
        # Same deployment name on another account must not leak its model.
        _deployment("new-unsynced", "gpt-35-turbo", resource_id=EASTUS + "-other"),
    ]
    usage = metrics.parse_metrics(SUB, EASTUS, deployments, load("metrics_eastus.json"))
    got = {(u.deployment, u.bucket_start.hour): u for u in usage}
    assert set(got) == {
        ("search-chat-gpt4o", 0),
        ("search-chat-gpt4o", 1),
        ("search-embed", 0),
        ("new-unsynced", 1),
    }
    chat0 = got[("search-chat-gpt4o", 0)]
    assert (chat0.input_tokens, chat0.output_tokens, chat0.requests) == (120000, 15000, 240)
    assert chat0.model == "gpt-4o"
    assert chat0.bucket_start == datetime(2026, 10, 12, 0, tzinfo=UTC)
    chat1 = got[("search-chat-gpt4o", 1)]
    assert (chat1.input_tokens, chat1.output_tokens, chat1.requests) == (98000, 0, 190)
    embed = got[("search-embed", 0)]
    assert (embed.input_tokens, embed.requests, embed.model) == (
        500000,
        1000,
        "text-embedding-3-small",
    )
    assert got[("new-unsynced", 1)].model is None
    assert all(u.source == "metrics" and u.resource_id == EASTUS for u in usage)


def test_parse_metrics_ignores_unknown_metrics_and_empty_payload():
    assert metrics.parse_metrics(SUB, EASTUS, [], {}) == []
    payload = {"value": [{"name": {"value": "TokenTransaction"}, "timeseries": []}]}
    assert metrics.parse_metrics(SUB, EASTUS, [], payload) == []
