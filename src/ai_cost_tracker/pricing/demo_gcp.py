"""List prices for the FakeGcpConnector's SKUs, so GCP demo estimates and reconciliation work
without calling the Cloud Billing Catalog API.

Prices are USD (like the catalog sync); the demo project bills in EUR at ``DEMO_EUR_PER_USD``,
so estimates reconcile only after FX conversion, which is what the demo is meant to show.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.connectors.fake_gcp import DEMO_EUR_PER_USD, LOCATION, demo_gcp_price_points
from ai_cost_tracker.db.models import Price
from ai_cost_tracker.normalise import parse_meter

_VALID_FROM = date(2025, 1, 1)

__all__ = ["DEMO_EUR_PER_USD", "demo_gcp_prices", "seed_gcp_demo_prices"]


def demo_gcp_prices() -> dict[str, Decimal]:
    """SKU description -> USD per 1M tokens."""
    return demo_gcp_price_points()


async def seed_gcp_demo_prices(session: AsyncSession) -> int:
    """Replace the GCP demo price rows. Commits; returns the number of rows written."""
    prices = demo_gcp_prices()
    await session.execute(
        delete(Price).where(
            Price.cloud == "gcp", Price.sku_meter.in_(list(prices)), Price.region == LOCATION
        )
    )
    for sku, unit_price in prices.items():
        info = parse_meter("gcp", sku)
        session.add(
            Price(
                cloud="gcp",
                sku_meter=sku,
                region=LOCATION,
                unit="1M",
                unit_price=unit_price,
                currency="USD",
                valid_from=_VALID_FROM,
                tokens_per_unit=1_000_000,
                ai_model=info.model if info else None,
                ai_direction=info.direction if info else None,
                ai_deployment_type=info.deployment_type if info else None,
            )
        )
    await session.commit()
    return len(prices)
