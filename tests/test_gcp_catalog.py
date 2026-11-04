import copy
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from ai_cost_tracker.db.models import DailyEstimate, DeploymentRow, Price, TokenUsageRow
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.pricing import gcp_catalog
from ai_cost_tracker.pricing.gcp_catalog import sync_gcp_prices, unit_conversion

FIXTURES = Path(__file__).parent / "fixtures" / "gcp" / "catalog"
SERVICE = "C7E2-9256-1C43"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    """Stands in for GcpClient: serves the catalog fixtures and records each GET."""

    def __init__(self, skus: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.skus = skus or [_load("skus_page1"), _load("skus_page2")]

    async def get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((url, params))
        page = 1 if params.get("pageToken") else 0
        if url == f"{gcp_catalog.CATALOG_URL}/services":
            return _load(f"services_page{page + 1}")
        if url == f"{gcp_catalog.CATALOG_URL}/services/{SERVICE}/skus":
            return self.skus[page]
        raise AssertionError(f"unexpected url {url}")


async def _prices(session) -> dict[tuple[str, str], list[Price]]:
    out: dict[tuple[str, str], list[Price]] = {}
    rows = await session.scalars(select(Price).order_by(Price.valid_from))
    for p in rows:
        out.setdefault((p.sku_meter, p.region), []).append(p)
    return out


async def test_sync_finds_service_paginates_and_parses(session):
    client = FakeClient()
    n = await sync_gcp_prices(session, client, today=date(2025, 7, 1))

    urls = [(u.rsplit("/v1/", 1)[1], p.get("pageToken")) for u, p in client.calls]
    assert urls == [
        ("services", None),
        ("services", "svc-2"),
        (f"services/{SERVICE}/skus", None),
        (f"services/{SERVICE}/skus", "page-2"),
    ]
    assert all(p["currencyCode"] == "USD" for u, p in client.calls if u.endswith("/skus"))

    prices = await _prices(session)
    assert n == sum(len(v) for v in prices.values())
    # Unmapped node-hour, committed-use and EUR SKUs are dropped.
    assert {k[0] for k in prices} == {
        "Gemini 2.5 Flash Text Input - Predictions",
        "Gemini 2.5 Flash Text Output - Predictions",
        "Gemini 2.5 Pro Text Input - Predictions",
        "Gemini 2.5 Pro Text Output - Predictions",
        "Gemini 2.0 Flash Text Input - Predictions",
        "Gemini 2.0 Flash Text Output - Predictions",
        "Gemini 2.5 Pro Text Input > 200K tokens - Predictions",
        "Gemini 2.5 Flash Image Input - Predictions",
        "Gemini 2.0 Flash Text Input - Batch Predictions",
        "Gemini 1.0 Pro Text Input Characters",
        "Imagen 3 Generation",
        "Text Embedding 004 Online Predictions",
    }
    assert all(p.cloud == "gcp" and p.currency == "USD" for v in prices.values() for p in v)

    # One row per service region.
    flash_in = prices[("Gemini 2.5 Flash Text Input - Predictions", "us-central1")][0]
    assert ("Gemini 2.5 Flash Text Input - Predictions", "europe-west4") in prices
    assert (flash_in.ai_model, flash_in.ai_direction, flash_in.ai_deployment_type) == (
        "gemini-2.5-flash",
        "input",
        "global",
    )
    assert (flash_in.unit, flash_in.tokens_per_unit) == ("1M", 1_000_000)
    assert flash_in.unit_price == Decimal("0.3")
    assert flash_in.valid_from == date(2025, 6, 17)

    # First non-zero tier, not the free tier or a volume discount.
    assert prices[("Gemini 2.5 Pro Text Input - Predictions", "us-central1")][
        0
    ].unit_price == Decimal("1.25")
    # "global" -> region ""; per-token and per-1K units normalised to per 1M.
    g_in = prices[("Gemini 2.0 Flash Text Input - Predictions", "")][0]
    g_out = prices[("Gemini 2.0 Flash Text Output - Predictions", "")][0]
    assert (g_in.unit_price, g_in.tokens_per_unit) == (Decimal("0.1"), 1_000_000)
    assert (g_out.unit_price, g_out.unit) == (Decimal("0.4"), "1M")
    batch = prices[("Gemini 2.0 Flash Text Input - Batch Predictions", "us-central1")][0]
    assert (batch.ai_deployment_type, batch.unit_price) == ("batch", Decimal("0.05"))
    emb = prices[("Text Embedding 004 Online Predictions", "us-central1")][0]
    assert (emb.unit_price, emb.tokens_per_unit) == (Decimal("0.00002"), 1_000_000)

    # Tiers and modalities the estimator must never use as the base text rate.
    long_in = prices[("Gemini 2.5 Pro Text Input > 200K tokens - Predictions", "us-central1")][0]
    assert long_in.ai_direction == "input_long"
    image = prices[("Gemini 2.5 Flash Image Input - Predictions", "us-central1")][0]
    assert image.ai_direction == "image_input"
    chars = prices[("Gemini 1.0 Pro Text Input Characters", "us-central1")][0]
    assert (chars.unit, chars.tokens_per_unit, chars.unit_price) == (
        "k{char}",
        None,
        Decimal("0.000125"),
    )
    imagen = prices[("Imagen 3 Generation", "us-central1")][0]
    assert (imagen.ai_direction, imagen.tokens_per_unit, imagen.unit) == (
        "image_output",
        None,
        "count",
    )


async def test_sync_is_idempotent(session):
    first = await sync_gcp_prices(session, FakeClient(), today=date(2025, 7, 1))
    snapshot = {
        k: [(p.unit_price, p.valid_from) for p in v] for k, v in (await _prices(session)).items()
    }
    assert first > 0
    assert await sync_gcp_prices(session, FakeClient(), today=date(2025, 7, 2)) == 0
    after = {
        k: [(p.unit_price, p.valid_from) for p in v] for k, v in (await _prices(session)).items()
    }
    assert after == snapshot


def _with_price(sku_id: str, nanos: int, units: str, effective: str) -> list[dict[str, Any]]:
    pages = [_load("skus_page1"), _load("skus_page2")]
    for page in pages:
        for sku in page["skus"]:
            if sku["skuId"] == sku_id:
                info = sku["pricingInfo"][0]
                info["effectiveTime"] = effective
                rate = info["pricingExpression"]["tieredRates"][0]["unitPrice"]
                rate["units"], rate["nanos"] = units, nanos
    return pages


async def test_price_history(session):
    await sync_gcp_prices(session, FakeClient(), today=date(2025, 7, 1))
    key = ("Gemini 2.5 Flash Text Input - Predictions", "us-central1")

    # Same price re-published later: no new row.
    same = _with_price("A001-0001-0001", 300000000, "0", "2025-09-01T07:00:00Z")
    assert await sync_gcp_prices(session, FakeClient(same)) == 0

    # Price change: a new row from the new effective date; the old one stays.
    cheaper = _with_price("A001-0001-0001", 250000000, "0", "2025-10-01T07:00:00Z")
    assert await sync_gcp_prices(session, FakeClient(cheaper)) == 2  # both regions
    history = (await _prices(session))[key]
    assert [(p.valid_from, p.unit_price) for p in history] == [
        (date(2025, 6, 17), Decimal("0.3")),
        (date(2025, 10, 1), Decimal("0.25")),
    ]

    # Correction on the same effective date updates in place.
    fixed = _with_price("A001-0001-0001", 260000000, "0", "2025-10-01T07:00:00Z")
    assert await sync_gcp_prices(session, FakeClient(fixed)) == 2
    history = (await _prices(session))[key]
    assert [p.unit_price for p in history] == [Decimal("0.3"), Decimal("0.26")]


async def test_missing_service_raises(session):
    class NoVertex(FakeClient):
        async def get(self, url, params=None):
            if url.endswith("/services"):
                return {"services": [{"serviceId": "X", "displayName": "Compute Engine"}]}
            return await super().get(url, params)

    with pytest.raises(LookupError, match="Vertex AI"):
        await sync_gcp_prices(session, NoVertex())


def test_effective_time_missing_uses_today():
    raw = copy.deepcopy(_load("skus_page1")["skus"][0])
    del raw["pricingInfo"][0]["effectiveTime"]
    items = gcp_catalog.parse_catalog_sku(raw, date(2025, 7, 1))
    assert {i.valid_from for i in items} == {date(2025, 7, 1)}


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ({"usageUnit": "count"}, ("1M", 1_000_000, Decimal(1_000_000))),
        ({"usageUnit": "{token}"}, ("1M", 1_000_000, Decimal(1_000_000))),
        ({"usageUnit": "k{token}"}, ("1M", 1_000_000, Decimal(1_000))),
        ({"usageUnit": "1M{token}"}, ("1M", 1_000_000, Decimal(1))),
        ({"usageUnit": "M{token}"}, ("1M", 1_000_000, Decimal(1))),
        (
            {"usageUnit": "1/1M", "baseUnit": "count", "baseUnitConversionFactor": 1000000},
            ("1M", 1_000_000, Decimal(1)),
        ),
        ({"usageUnit": "k{char}"}, ("k{char}", None, Decimal(1))),
        (
            {"usageUnit": "count", "usageUnitDescription": "1000 characters"},
            ("count", None, Decimal(1)),
        ),
        ({"usageUnit": "h"}, ("h", None, Decimal(1))),
    ],
)
def test_unit_conversion(expr, expected):
    assert unit_conversion(expr) == expected


def test_unit_conversion_not_token_priced():
    assert unit_conversion({"usageUnit": "count"}, token_priced=False) == ("count", None, 1)


async def test_synced_prices_feed_estimates(session, tenant_id: uuid.UUID):
    """Catalog rows price GCP publisher-model tokens (sku None -> global) per region."""
    await sync_gcp_prices(session, FakeClient(), today=date(2025, 7, 1))
    rid = "//aiplatform.googleapis.com/projects/demo/locations/us-central1"
    session.add(
        DeploymentRow(
            tenant_id=tenant_id,
            cloud="gcp",
            account_id="demo",
            resource_id=rid,
            name="gemini-2.5-pro",
            model="gemini-2.5-pro",
            sku=None,
            region="us-central1",
        )
    )
    session.add(
        TokenUsageRow(
            tenant_id=tenant_id,
            cloud="gcp",
            account_id="demo",
            resource_id=rid,
            deployment="gemini-2.5-pro",
            model="gemini-2.5-pro",
            bucket_start=datetime(2025, 7, 1, 10, tzinfo=UTC),
            input_tokens=1_000_000,
            output_tokens=100_000,
            requests=10,
            source="test",
        )
    )
    await session.commit()
    assert await compute_estimates(session, tenant_id, date(2025, 7, 1), date(2025, 7, 1)) == 1
    est = await session.scalar(select(DailyEstimate))
    # 1M x 1.25 + 0.1M x 10.00; the > 200K tier is not the base rate.
    assert (est.input_cost, est.output_cost) == (Decimal("1.25"), Decimal("1"))
    assert not est.price_missing
