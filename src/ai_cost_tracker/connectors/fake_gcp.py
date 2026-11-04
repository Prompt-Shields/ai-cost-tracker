"""Deterministic demo GCP project, so the multi-cloud overview runs without a GCP project.

Mirrors ``FakeConnector``: same day-to-day variation, today never billed. One difference on
purpose: the project bills in EUR (converted from USD list prices at ``DEMO_EUR_PER_USD``),
so the demo exercises FX conversion and estimates (USD) reconcile only once converted.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.connectors.fake import _WEEKDAY_CURVE, _WEEKEND_CURVE, _day_factor
from ai_cost_tracker.connectors.gcp.ids import resource_id
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage
from ai_cost_tracker.normalise.gcp_skus import canonical_model, model_version

PROJECT = "demo-gcp-project"
LOCATION = "us-central1"
SERVICE = "Vertex AI"
# EUR per USD for the demo bill; fx.seed_demo_fx seeds the matching rate.
DEMO_EUR_PER_USD = Decimal("0.92")

_MONEY = Decimal("0.0000000001")
_MILLION = Decimal(1_000_000)

# (deployment name as Cloud Monitoring's model_user_id reports it, SKU model words,
#  peak hourly input tokens, peak hourly output tokens, $ per 1M input, $ per 1M output, tags).
# Sized so GCP is a visible but minor share next to the Azure demo, with 2.5 Pro the biggest.
_DEPLOYMENTS = [
    (
        "gemini-2.5-flash",
        "Gemini 2.5 Flash",
        1_500_000,
        150_000,
        "0.30",
        "2.50",
        {"team": "search", "env": "prod"},
    ),
    (
        "gemini-2.5-pro",
        "Gemini 2.5 Pro",
        4_000_000,
        600_000,
        "1.25",
        "10.00",
        {"team": "research", "env": "prod"},
    ),
    (
        "gemini-2.0-flash-001",
        "Gemini 2.0 Flash",
        2_000_000,
        300_000,
        "0.10",
        "0.40",
        {"team": "support", "env": "dev"},
    ),
]


def _sku(words: str, direction: str) -> str:
    return f"{words} Text {direction} - Predictions"


def _hourly_tokens(spec: tuple, hour: datetime) -> tuple[int, int]:
    """Repeatable (input, output) tokens for one UTC hour: office-hours curve x day factor.

    The curve is floored at 30% of peak (Gemini traffic here is partly batch-like, so nights
    aren't idle). Integer per hour, and the daily bill sums these same integers.
    """
    name, _words, tin, tout, *_rest = spec
    curve = _WEEKEND_CURVE if hour.weekday() >= 5 else _WEEKDAY_CURVE
    shape = (Decimal(30) + Decimal(curve[hour.hour]) * Decimal("0.7")) / 100
    factor = _day_factor(hour.date(), name) * shape
    return int(tin * factor), int(tout * factor)


def _day_hours(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [start + timedelta(hours=h) for h in range(24)]


class FakeGcpConnector:
    name = "fake_gcp"
    cloud = "gcp"

    def __init__(self, today: date | None = None) -> None:
        self._fixed_today = today

    def _today(self) -> date:
        return getattr(self, "_fixed_today", None) or datetime.now(UTC).date()

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        return [
            CloudAccount(
                cloud="gcp", account_id=PROJECT, name="Demo GCP project", has_ai_resources=True
            )
        ]

    async def check_access(self, account_id: str) -> bool:
        return True

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return [
            Deployment(
                cloud="gcp",
                account_id=account_id,
                resource_id=resource_id(account_id, LOCATION),
                name=name,
                model=canonical_model(name),
                model_version=model_version(name),
                sku=None,
                region=LOCATION,
                tags=dict(tags),
            )
            for name, *_rest, tags in _DEPLOYMENTS
        ]

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        # Like real billing export, today isn't billed yet: it shows as an estimate until then.
        end = min(end, self._today() - timedelta(days=1))
        rid = resource_id(account_id, LOCATION)
        records: list[CostRecord] = []
        day = start
        while day <= end:
            hours = _day_hours(day)
            for spec in _DEPLOYMENTS:
                _name, words, _tin, _tout, pin, pout, _tags = spec
                per_hour = [_hourly_tokens(spec, h) for h in hours]
                tokens_in = sum(i for i, _ in per_hour)
                tokens_out = sum(o for _, o in per_hour)
                for direction, tokens, price in (
                    ("Input", tokens_in, pin),
                    ("Output", tokens_out, pout),
                ):
                    qty = Decimal(tokens) / _MILLION
                    eur = qty * Decimal(price) * DEMO_EUR_PER_USD
                    records.append(
                        CostRecord(
                            cloud="gcp",
                            sub_account_id=account_id,
                            resource_id=rid,
                            charge_period_start=day,
                            service_name=SERVICE,
                            sku_meter=_sku(words, direction),
                            consumed_quantity=qty,
                            billed_cost=eur.quantize(_MONEY),
                            billing_currency="EUR",
                        )
                    )
            day += timedelta(days=1)
        return records

    async def pull_usage(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[TokenUsage]:
        by_name = {d[0]: d for d in _DEPLOYMENTS}
        out: list[TokenUsage] = []
        hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        while hour < end:
            for dep in deployments:
                spec = by_name.get(dep.name)
                if spec is None:
                    continue
                tokens_in, tokens_out = _hourly_tokens(spec, hour)
                out.append(
                    TokenUsage(
                        cloud="gcp",
                        account_id=account_id,
                        resource_id=dep.resource_id,
                        deployment=dep.name,
                        model=dep.model,
                        bucket_start=hour,
                        input_tokens=tokens_in,
                        output_tokens=tokens_out,
                        requests=tokens_in // 2_000,
                    )
                )
            hour += timedelta(hours=1)
        return out

    async def pull_ptu_utilization(
        self, account_id: str, deployments: list[Deployment], start: datetime, end: datetime
    ) -> list[PtuSample]:
        return []  # Vertex AI provisioned throughput isn't modelled.


def demo_gcp_price_points() -> dict[str, Decimal]:
    """USD list price per 1M tokens for each demo SKU description, for ``pricing.demo_gcp``."""
    points: dict[str, Decimal] = {}
    for _name, words, _tin, _tout, pin, pout, _tags in _DEPLOYMENTS:
        points[_sku(words, "Input")] = Decimal(pin)
        points[_sku(words, "Output")] = Decimal(pout)
    return points
