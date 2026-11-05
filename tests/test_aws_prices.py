import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from ai_cost_tracker.db.models import DailyEstimate, DeploymentRow, Price, TokenUsageRow
from ai_cost_tracker.estimate import compute_estimates
from ai_cost_tracker.pricing import aws_prices
from ai_cost_tracker.pricing.aws_prices import sync_aws_prices, unit_conversion

FIXTURES = Path(__file__).parent / "fixtures" / "aws" / "prices"
HOST = "pricing.us-east-1.amazonaws.com"
FILES = {
    "/offers/v1.0/aws/AmazonBedrock/current/region_index.json": "bedrock_region_index",
    "/offers/v1.0/aws/AmazonBedrock/20250601000000/us-east-1/index.json": "bedrock_us-east-1",
    "/offers/v1.0/aws/AmazonBedrock/20250601000000/us-west-2/index.json": "bedrock_us-west-2",
    "/offers/v1.0/aws/AmazonBedrockFoundationModels/current/region_index.json": "fm_region_index",
    "/offers/v1.0/aws/AmazonBedrockFoundationModels/20250601000000/us-east-1/index.json": (
        "fm_us-east-1"
    ),
}
REGIONS = ["us-east-1", "us-west-2", "eu-central-1"]


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class PriceList:
    """Fake bulk price list: serves fixtures, records paths, can override bodies/statuses."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.paths: list[str] = []
        self.overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == HOST
        path = request.url.path
        self.paths.append(path)
        override = self.overrides.get(path)
        if isinstance(override, int):
            return httpx.Response(override, text="<Error>AccessDenied</Error>")
        if override is not None:
            return httpx.Response(200, json=override)
        if path not in FILES:
            return httpx.Response(403, text="<Error>AccessDenied</Error>")
        return httpx.Response(200, json=_load(FILES[path]))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _prices(session) -> dict[tuple[str, str], list[Price]]:
    out: dict[tuple[str, str], list[Price]] = {}
    for p in await session.scalars(select(Price).order_by(Price.valid_from)):
        out.setdefault((p.sku_meter, p.region), []).append(p)
    return out


async def test_sync_reads_region_index_and_offer_files(session):
    fake = PriceList()
    n = await sync_aws_prices(session, fake.client(), regions=REGIONS, today=date(2025, 7, 1))

    assert fake.paths == [
        "/offers/v1.0/aws/AmazonBedrock/current/region_index.json",
        "/offers/v1.0/aws/AmazonBedrock/20250601000000/us-east-1/index.json",
        "/offers/v1.0/aws/AmazonBedrock/20250601000000/us-west-2/index.json",
        "/offers/v1.0/aws/AmazonBedrockFoundationModels/current/region_index.json",
        "/offers/v1.0/aws/AmazonBedrockFoundationModels/20250601000000/us-east-1/index.json",
    ]  # eu-central-1 isn't listed: skipped; ap-south-1 isn't wanted: never fetched.
    prices = await _prices(session)
    assert n == sum(len(v) for v in prices.values())
    assert all(p.cloud == "aws" and p.currency == "USD" for v in prices.values() for p in v)
    # Region-stripped usage types; unmapped (Marketplace, storage) and priceless dropped.
    assert set(prices) == {
        ("NovaPro-input-tokens", "us-east-1"),
        ("NovaPro-output-tokens", "us-east-1"),
        ("NovaPro-input-tokens", "us-west-2"),
        ("NovaPro-output-tokens", "us-west-2"),
        ("NovaLite-input-tokens-batch", "us-east-1"),
        ("NovaMicro-input-tokens", "us-east-1"),
        ("TitanEmbeddingsV2-Text-input-tokens", "us-east-1"),
        ("TitanImageGeneratorG1-1024-standard-images", "us-east-1"),
        ("NovaSonic-audio-input-tokens", "us-east-1"),
        ("Llama3.1-70B-input-tokens", "us-east-1"),
        ("Llama3.3-70B-input-tokens", "us-east-1"),
        ("Claude3.7Sonnet-ProvisionedThroughput-ModelUnits", "us-east-1"),
        ("Claude3.7Sonnet-input-tokens", "us-east-1"),
        ("Claude3.7Sonnet-output-tokens", "us-east-1"),
        ("Claude3.7Sonnet-cache-read-input-tokens", "us-east-1"),
        ("Claude3.7Sonnet-input-tokens-long-context", "us-east-1"),
        ("Claude3.5Haiku-input-tokens", "us-east-1"),
        ("Claude3.5Haiku-output-tokens", "us-east-1"),
    }

    def one(sku: str, region: str = "us-east-1") -> Price:
        (row,) = prices[(sku, region)]
        return row

    nova_in = one("NovaPro-input-tokens")
    assert (nova_in.ai_model, nova_in.ai_direction, nova_in.ai_deployment_type) == (
        "nova-pro",
        "input",
        "global",
    )
    # Per 1K tokens normalised to per 1M.
    assert (nova_in.unit, nova_in.tokens_per_unit, nova_in.unit_price) == (
        "1M",
        1_000_000,
        Decimal("0.8"),
    )
    assert nova_in.valid_from == date(2025, 2, 1)
    assert one("NovaPro-output-tokens", "us-west-2").unit_price == Decimal("3.2")
    assert one("Claude3.7Sonnet-output-tokens").unit_price == Decimal("15")
    # Already per 1M (third-party offer); effective date differs per product.
    haiku = one("Claude3.5Haiku-input-tokens")
    assert (haiku.ai_model, haiku.unit_price, haiku.unit) == (
        "claude-3-5-haiku",
        Decimal("0.8"),
        "1M",
    )
    micro = one("NovaMicro-input-tokens")
    assert (micro.unit_price, micro.valid_from) == (Decimal("0.035"), date(2025, 4, 15))
    # Free tier skipped; reserved terms ignored.
    assert one("Llama3.3-70B-input-tokens").unit_price == Decimal("0.72")
    llama = one("Llama3.1-70B-input-tokens")
    assert (llama.ai_model, llama.unit_price) == ("llama3-1-70b-instruct", Decimal("0.72"))
    batch = one("NovaLite-input-tokens-batch")
    assert (batch.ai_deployment_type, batch.unit_price) == ("batch", Decimal("0.03"))
    emb = one("TitanEmbeddingsV2-Text-input-tokens")
    assert (emb.ai_direction, emb.unit_price) == ("input", Decimal("0.02"))

    # Rows the estimator must never use as the base text rate.
    assert one("Claude3.7Sonnet-cache-read-input-tokens").ai_direction == "cached_input"
    assert one("Claude3.7Sonnet-input-tokens-long-context").ai_direction == "input_long"
    assert one("NovaSonic-audio-input-tokens").ai_direction == "audio_input"
    image = one("TitanImageGeneratorG1-1024-standard-images")
    assert (image.ai_direction, image.unit, image.tokens_per_unit, image.unit_price) == (
        "image_output",
        "Image",
        None,
        Decimal("0.01"),
    )
    pt = one("Claude3.7Sonnet-ProvisionedThroughput-ModelUnits")
    assert (pt.ai_direction, pt.ai_deployment_type, pt.unit, pt.tokens_per_unit) == (
        "provisioned",
        "provisioned",
        "Hrs",
        None,
    )


async def test_sync_is_idempotent(session):
    first = await sync_aws_prices(session, PriceList().client(), regions=REGIONS)
    snapshot = {
        k: [(p.unit_price, p.valid_from) for p in v] for k, v in (await _prices(session)).items()
    }
    assert first > 0
    again = await sync_aws_prices(
        session, PriceList().client(), regions=REGIONS, today=date(2025, 8, 1)
    )
    assert again == 0
    after = {
        k: [(p.unit_price, p.valid_from) for p in v] for k, v in (await _prices(session)).items()
    }
    assert after == snapshot


def _with_price(usd: str, effective: str) -> dict[str, Any]:
    """us-east-1 Bedrock offer with NovaPro input re-priced."""
    body = _load("bedrock_us-east-1")
    (sku,) = [
        s
        for s, p in body["products"].items()
        if p["attributes"]["usagetype"] == "USE1-NovaPro-input-tokens"
    ]
    for term in body["terms"]["OnDemand"][sku].values():
        term["effectiveDate"] = effective
        for dim in term["priceDimensions"].values():
            dim["pricePerUnit"]["USD"] = usd
    return body


async def test_price_history(session):
    path = "/offers/v1.0/aws/AmazonBedrock/20250601000000/us-east-1/index.json"
    key = ("NovaPro-input-tokens", "us-east-1")
    await sync_aws_prices(session, PriceList().client(), regions=["us-east-1"])

    # Same price re-published later: no new row.
    same = PriceList({path: _with_price("0.0008000000", "2025-09-01T00:00:00Z")})
    assert await sync_aws_prices(session, same.client(), regions=["us-east-1"]) == 0

    # Price change: a new row from the new effective date; the old one stays.
    cheaper = PriceList({path: _with_price("0.0006000000", "2025-10-01T00:00:00Z")})
    assert await sync_aws_prices(session, cheaper.client(), regions=["us-east-1"]) == 1
    history = (await _prices(session))[key]
    assert [(p.valid_from, p.unit_price) for p in history] == [
        (date(2025, 2, 1), Decimal("0.8")),
        (date(2025, 10, 1), Decimal("0.6")),
    ]

    # Correction on the same effective date updates in place.
    fixed = PriceList({path: _with_price("0.0006500000", "2025-10-01T00:00:00Z")})
    assert await sync_aws_prices(session, fixed.client(), regions=["us-east-1"]) == 1
    history = (await _prices(session))[key]
    assert [p.unit_price for p in history] == [Decimal("0.8"), Decimal("0.65")]


async def test_default_region_and_optional_offer(session):
    fm = "/offers/v1.0/aws/AmazonBedrockFoundationModels/current/region_index.json"
    fake = PriceList({fm: 404})
    assert await sync_aws_prices(session, fake.client()) > 0
    regions = {region for _, region in await _prices(session)}
    assert regions == {"us-east-1"}
    assert all("us-west-2" not in p for p in fake.paths)
    assert ("Claude3.5Haiku-input-tokens", "us-east-1") not in await _prices(session)


async def test_missing_bedrock_offer_raises(session):
    fake = PriceList({"/offers/v1.0/aws/AmazonBedrock/current/region_index.json": 403})
    with pytest.raises(LookupError, match="AmazonBedrock"):
        await sync_aws_prices(session, fake.client())


async def test_retries_on_throttling(session, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(aws_prices.asyncio, "sleep", fake_sleep)
    fake = PriceList()
    attempts = {"n": 0}
    index = "/offers/v1.0/aws/AmazonBedrock/current/region_index.json"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == index and attempts["n"] < 2:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"})
            return httpx.Response(503)
        return fake.handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await sync_aws_prices(session, client, regions=["us-east-1"]) > 0
    assert sleeps == [3.0, 2.0]


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("1K tokens", (1_000_000, Decimal(1_000), "1M")),
        ("1K Tokens", (1_000_000, Decimal(1_000), "1M")),
        ("1M tokens", (1_000_000, Decimal(1), "1M")),
        ("Million tokens", (1_000_000, Decimal(1), "1M")),
        ("1K input tokens", (1_000_000, Decimal(1_000), "1M")),
        ("Tokens", (1_000_000, Decimal(1_000_000), "1M")),
        ("Hrs", (None, Decimal(1), "Hrs")),
        ("Image", (None, Decimal(1), "Image")),
    ],
)
def test_unit_conversion(unit, expected):
    assert unit_conversion(unit) == expected


def test_unit_conversion_not_token_priced():
    assert unit_conversion("1K tokens", token_priced=False) == (None, Decimal(1), "1K tokens")


async def test_synced_prices_feed_estimates(session, tenant_id: uuid.UUID):
    """Price-list rows price a cross-region Bedrock profile (sku None -> global) per region."""
    await sync_aws_prices(session, PriceList().client(), regions=REGIONS, today=date(2025, 7, 1))
    rid = "//bedrock.amazonaws.com/accounts/123456789012/regions/us-east-1"
    name = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
    session.add(
        DeploymentRow(
            tenant_id=tenant_id,
            cloud="aws",
            account_id="123456789012",
            resource_id=rid,
            name=name,
            model="claude-3-7-sonnet",
            sku=None,
            region="us-east-1",
        )
    )
    session.add(
        TokenUsageRow(
            tenant_id=tenant_id,
            cloud="aws",
            account_id="123456789012",
            resource_id=rid,
            deployment=name,
            model="claude-3-7-sonnet",
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
    # 1M x 3.00 + 0.1M x 15.00; cache-read and long-context rows are not the base rate.
    assert (est.input_cost, est.output_cost) == (Decimal("3"), Decimal("1.5"))
    assert not est.price_missing
