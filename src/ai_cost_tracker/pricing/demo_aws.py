"""List prices for the FakeAwsConnector's usage types, so AWS demo estimates and reconciliation
work without downloading the public Bedrock price list.

The demo account bills in USD at exactly these prices, so estimates reconcile to 0%.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.connectors.fake_aws import REGION, demo_aws_price_points
from ai_cost_tracker.db.models import Price
from ai_cost_tracker.normalise import parse_meter

_VALID_FROM = date(2025, 1, 1)

__all__ = ["demo_aws_prices", "seed_aws_demo_prices"]


def demo_aws_prices() -> dict[str, Decimal]:
    """Region-stripped usage type -> USD per 1M tokens."""
    return demo_aws_price_points()


async def seed_aws_demo_prices(session: AsyncSession) -> int:
    """Replace the AWS demo price rows. Commits; returns the number of rows written."""
    prices = demo_aws_prices()
    await session.execute(
        delete(Price).where(
            Price.cloud == "aws", Price.sku_meter.in_(list(prices)), Price.region == REGION
        )
    )
    for sku, unit_price in prices.items():
        info = parse_meter("aws", sku)
        session.add(
            Price(
                cloud="aws",
                sku_meter=sku,
                region=REGION,
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
