"""BigQuery billing export: query text and parameters, polling, paging, row decoding."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ai_cost_tracker.connectors.gcp import billing
from ai_cost_tracker.connectors.gcp.ids import resource_id

FIXTURES = Path(__file__).parent.parent / "fixtures" / "gcp" / "billing"
TABLE = "billing-proj.billing_ds.gcp_billing_export_resource_v1_0123AB_CDEF45_6789GH"
PROJECT = "demo-gcp-project"
BQ = "https://bigquery.googleapis.com/bigquery/v2"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeClient:
    """Replays responses in order and records every call (no HTTP at all)."""

    def __init__(self, post: dict, gets: list[dict] | None = None) -> None:
        self._post = post
        self._gets = list(gets or [])
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def post(self, url: str, json: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append(("POST", url, json))
        return self._post

    async def get(self, url: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", url, params))
        return self._gets.pop(0)


def params_by_name(params: list[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in params}


def test_build_query_text_and_params():
    sql, params = billing.build_query(TABLE, PROJECT, date(2026, 10, 1), date(2026, 10, 3))
    assert f"FROM `{TABLE}`" in sql
    assert "project.id = @project" in sql
    assert "service.description IN UNNEST(@services)" in sql
    assert "IFNULL(cost_type, 'regular') != 'tax'" in sql
    assert "DATE(usage_start_time) AS day" in sql
    assert "usage_start_time < TIMESTAMP(DATE_ADD(@end, INTERVAL 1 DAY))" in sql
    assert "_PARTITIONTIME >= TIMESTAMP(DATE_SUB(@start, INTERVAL 2 DAY))" in sql
    assert "UNNEST(credits)" in sql
    assert "GROUP BY day, location, sku, service, currency" in sql
    assert PROJECT not in sql  # values only travel as parameters

    p = params_by_name(params)
    assert p["project"] == {
        "name": "project",
        "parameterType": {"type": "STRING"},
        "parameterValue": {"value": PROJECT},
    }
    assert p["start"]["parameterType"] == {"type": "DATE"}
    assert p["start"]["parameterValue"] == {"value": "2026-10-01"}
    assert p["end"]["parameterValue"] == {"value": "2026-10-03"}
    assert p["services"]["parameterType"] == {"type": "ARRAY", "arrayType": {"type": "STRING"}}
    assert p["services"]["parameterValue"] == {"arrayValues": [{"value": "Vertex AI"}]}


@pytest.mark.parametrize(
    "table",
    [
        "",
        "dataset.table",
        "a.b.c.d",
        "proj.ds.tbl` WHERE 1=1; --",
        "proj.ds.tbl`",
        "proj.ds.t bl",
        "proj.ds.'tbl'",
        "proj..tbl",
        "proj.ds.tbl\n",
        "proj.ds.tâble",
    ],
)
def test_invalid_table_names_rejected(table):
    with pytest.raises(ValueError, match="ACT_GCP_BILLING_EXPORT_TABLE"):
        billing.build_query(table, PROJECT, date(2026, 10, 1), date(2026, 10, 1))


def test_valid_table_with_dashes_accepted():
    assert billing.validate_table("my-proj.ds_1.tbl-x") == ("my-proj", "ds_1", "tbl-x")


def test_end_before_start_rejected():
    with pytest.raises(ValueError, match="before start"):
        billing.build_query(TABLE, PROJECT, date(2026, 10, 2), date(2026, 10, 1))


async def test_pull_cost_polls_then_pages():
    client = FakeClient(
        load("query_running.json"), [load("results_page1.json"), load("results_page2.json")]
    )
    records = await billing.pull_cost(
        client,
        table=TABLE,
        location="US",
        project_id=PROJECT,
        start=date(2026, 10, 1),
        end=date(2026, 10, 2),
    )

    method, url, body = client.calls[0]
    assert (method, url) == ("POST", f"{BQ}/projects/billing-proj/queries")
    assert body["useLegacySql"] is False
    assert body["parameterMode"] == "NAMED"
    assert body["location"] == "US"
    assert body["query"].startswith("SELECT")
    assert params_by_name(body["queryParameters"])["project"]["parameterValue"]["value"] == PROJECT

    job_url = f"{BQ}/projects/billing-proj/queries/job_abc123"
    # First GET is the poll (no page token), second fetches page 2.
    assert client.calls[1][:2] == ("GET", job_url)
    assert client.calls[1][2]["location"] == "US"
    assert "pageToken" not in client.calls[1][2]
    assert client.calls[2][:2] == ("GET", job_url)
    assert client.calls[2][2]["pageToken"] == "tok-2"
    assert len(client.calls) == 3

    assert len(records) == 4
    first = records[0]
    assert first.cloud == "gcp"
    assert first.sub_account_id == PROJECT
    assert first.resource_id == resource_id(PROJECT, "us-central1")
    assert first.charge_period_start == date(2026, 10, 1)
    assert first.service_name == "Vertex AI"
    assert first.sku_meter == "Gemini 2.5 Pro Text Input - Predictions"
    assert first.billing_currency == "EUR"
    assert first.billed_cost == Decimal("10.345678901")  # 12.345678901 net of -2 credits
    assert first.consumed_quantity == Decimal("9876.5")

    assert records[1].billed_cost == Decimal("40.1")
    assert records[1].consumed_quantity == Decimal("4010.000000001")


async def test_missing_location_is_global_and_null_credits_and_quantity():
    client = FakeClient(load("results_page1.json"), [load("results_page2.json")])
    records = await billing.pull_cost(
        client,
        table=TABLE,
        location="US",
        project_id=PROJECT,
        start=date(2026, 10, 1),
        end=date(2026, 10, 2),
    )
    assert len(client.calls) == 2  # already complete: no poll, one page fetch
    flash = records[2]
    assert flash.resource_id == resource_id(PROJECT, None)
    assert flash.resource_id.endswith("/locations/global")
    assert flash.billed_cost == Decimal("0.000000001")  # exact, no float rounding
    assert flash.consumed_quantity is None


async def test_fully_credited_rows_are_kept_as_zero():
    client = FakeClient(load("results_page2.json"))
    records = await billing.pull_cost(
        client,
        table=TABLE,
        location="US",
        project_id=PROJECT,
        start=date(2026, 10, 2),
        end=date(2026, 10, 2),
    )
    assert len(records) == 1
    assert records[0].billed_cost == Decimal("0")
    assert records[0].resource_id == resource_id(PROJECT, "global")


async def test_empty_result():
    page = {"jobReference": {"jobId": "j"}, "jobComplete": True, "totalRows": "0"}
    client = FakeClient(page)
    records = await billing.pull_cost(
        client,
        table=TABLE,
        location="EU",
        project_id=PROJECT,
        start=date(2026, 10, 1),
        end=date(2026, 10, 1),
    )
    assert records == []
    assert client.calls[0][2]["location"] == "EU"


async def test_poll_gives_up(monkeypatch):
    monkeypatch.setattr(billing, "MAX_POLLS", 2)
    running = load("query_running.json")
    client = FakeClient(running, [running, running, running])
    with pytest.raises(billing.BillingQueryError, match="did not complete"):
        await billing.pull_cost(
            client,
            table=TABLE,
            location="US",
            project_id=PROJECT,
            start=date(2026, 10, 1),
            end=date(2026, 10, 1),
        )
    assert len(client.calls) == 3  # POST + 2 polls


async def test_invalid_table_makes_no_calls():
    client = FakeClient({})
    with pytest.raises(ValueError):
        await billing.pull_cost(
            client,
            table="x.y.z`; DROP",
            location="US",
            project_id=PROJECT,
            start=date(2026, 10, 1),
            end=date(2026, 10, 1),
        )
    assert client.calls == []


def test_decode_row_types():
    schema = {
        "fields": [
            {"name": "d", "type": "DATE"},
            {"name": "f", "type": "FLOAT"},
            {"name": "n", "type": "NUMERIC"},
            {"name": "i", "type": "INTEGER"},
            {"name": "t", "type": "TIMESTAMP"},
            {"name": "b", "type": "BOOLEAN"},
            {"name": "s", "type": "STRING"},
            {"name": "x", "type": "NUMERIC"},
        ]
    }
    row = {
        "f": [
            {"v": "2026-10-01"},
            {"v": "1.0E-5"},
            {"v": "0.1"},
            {"v": "42"},
            {"v": "1.7597088E9"},
            {"v": "true"},
            {"v": "abc"},
            {"v": None},
        ]
    }
    out = billing.decode_row(schema, row)
    assert out["d"] == date(2026, 10, 1)
    assert out["f"] == Decimal("0.00001")
    assert out["n"] == Decimal("0.1")
    assert out["i"] == 42
    assert out["t"].isoformat() == "2025-10-06T00:00:00+00:00"
    assert out["b"] is True
    assert out["s"] == "abc"
    assert out["x"] is None


def test_decode_row_rejects_shape_mismatch():
    with pytest.raises(billing.BillingQueryError):
        billing.decode_row({"fields": [{"name": "a", "type": "STRING"}]}, {"f": []})
