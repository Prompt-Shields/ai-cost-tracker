"""List prices for the FakeConnector's models, so demo estimates and reconciliation work
without calling the Retail Prices API."""

from datetime import date

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.connectors.fake import demo_price_points
from ai_cost_tracker.db.models import Price

_VALID_FROM = date(2024, 1, 1)


async def seed_demo_prices(session: AsyncSession) -> int:
    """Replace the demo price rows. Commits; returns the number of rows written."""
    points = demo_price_points()
    meters = [p["sku_meter"] for p in points]
    await session.execute(delete(Price).where(Price.cloud == "azure", Price.sku_meter.in_(meters)))
    session.add_all(
        Price(
            cloud="azure",
            region="",
            unit="1M",
            currency="USD",
            valid_from=_VALID_FROM,
            ai_deployment_type="global",
            tokens_per_unit=1_000_000,
            **p,
        )
        for p in points
    )
    await session.commit()
    return len(points)
