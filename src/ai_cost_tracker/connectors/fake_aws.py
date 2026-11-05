"""Deterministic demo AWS account, so the three-cloud overview runs without an AWS account.

Mirrors ``FakeGcpConnector``: same day-to-day variation, today never billed. Bills in USD
(like most AWS accounts), so estimates reconcile with no FX step. Usage types follow the
CUR ``line_item_usage_type`` spelling (Assumption (spike-aws)); ``sku_meter`` is the
region-stripped form, as the real billing connector stores it.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ai_cost_tracker.connectors.aws.ids import resource_id
from ai_cost_tracker.connectors.base import PtuSample
from ai_cost_tracker.connectors.fake import _WEEKDAY_CURVE, _WEEKEND_CURVE, _day_factor
from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage
from ai_cost_tracker.normalise.aws_usage import canonical_model

ACCOUNT = "123456789012"
ACCOUNT_NAME = "Demo AWS account"
REGION = "us-east-1"
REGION_PREFIX = "USE1-"  # CUR usage-type prefix for us-east-1.
SERVICE = "Amazon Bedrock"

_MONEY = Decimal("0.0000000001")
_MILLION = Decimal(1_000_000)

# (CloudWatch ModelId, model version, usage-type model words, peak hourly input tokens,
#  peak hourly output tokens, $ per 1M input, $ per 1M output, tags).
# Sized so AWS lands between the Azure token spend and the GCP demo, Sonnet the biggest.
_DEPLOYMENTS = [
    (
        "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "20250219",
        "Claude3.7Sonnet",
        1_200_000,
        120_000,
        "3.00",
        "15.00",
        {"team": "research", "env": "prod"},
    ),
    (
        "anthropic.claude-3-5-haiku-20241022-v1:0",
        "20241022",
        "Claude3.5Haiku",
        2_000_000,
        250_000,
        "0.80",
        "4.00",
        {"team": "support", "env": "prod"},
    ),
    (
        "amazon.nova-pro-v1:0",
        None,
        "NovaPro",
        800_000,
        80_000,
        "0.80",
        "3.20",
        {"team": "search", "env": "dev"},
    ),
]


def _usage_type(words: str, direction: str) -> str:
    """Region-stripped usage type, e.g. ``Claude3.7Sonnet-input-tokens``."""
    return f"{words}-{direction}-tokens"


def usage_types() -> list[str]:
    """The six CUR usage types the demo bills, with the region prefix (``USE1-...``)."""
    return [
        REGION_PREFIX + _usage_type(words, d)
        for _id, _ver, words, *_rest in _DEPLOYMENTS
        for d in ("input", "output")
    ]


def _hourly_tokens(spec: tuple, hour: datetime) -> tuple[int, int]:
    """Repeatable (input, output) tokens for one UTC hour: office-hours curve x day factor.

    Floored at 20% of peak (some always-on agents), so nights aren't idle. Integer per hour,
    and the daily bill sums these same integers, so estimates reconcile exactly.
    """
    name, _ver, _words, tin, tout, *_rest = spec
    curve = _WEEKEND_CURVE if hour.weekday() >= 5 else _WEEKDAY_CURVE
    shape = (Decimal(20) + Decimal(curve[hour.hour]) * Decimal("0.8")) / 100
    factor = _day_factor(hour.date(), name) * shape
    return int(tin * factor), int(tout * factor)


def _day_hours(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [start + timedelta(hours=h) for h in range(24)]


class FakeAwsConnector:
    name = "fake_aws"
    cloud = "aws"

    def __init__(self, today: date | None = None) -> None:
        self._fixed_today = today

    def _today(self) -> date:
        return getattr(self, "_fixed_today", None) or datetime.now(UTC).date()

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        return [
            CloudAccount(cloud="aws", account_id=ACCOUNT, name=ACCOUNT_NAME, has_ai_resources=True)
        ]

    async def check_access(self, account_id: str) -> bool:
        return True

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return [
            Deployment(
                cloud="aws",
                account_id=account_id,
                resource_id=resource_id(account_id, REGION),
                name=name,
                model=canonical_model(name),
                model_version=version,
                sku=None,
                region=REGION,
                tags=dict(tags),
            )
            for name, version, *_rest, tags in _DEPLOYMENTS
        ]

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        # Like the real CUR export, today isn't billed yet: it shows as an estimate until then.
        end = min(end, self._today() - timedelta(days=1))
        rid = resource_id(account_id, REGION)
        records: list[CostRecord] = []
        day = start
        while day <= end:
            hours = _day_hours(day)
            for spec in _DEPLOYMENTS:
                _name, _ver, words, _tin, _tout, pin, pout, _tags = spec
                per_hour = [_hourly_tokens(spec, h) for h in hours]
                tokens_in = sum(i for i, _ in per_hour)
                tokens_out = sum(o for _, o in per_hour)
                for direction, tokens, price in (
                    ("input", tokens_in, pin),
                    ("output", tokens_out, pout),
                ):
                    qty = Decimal(tokens) / _MILLION
                    records.append(
                        CostRecord(
                            cloud="aws",
                            sub_account_id=account_id,
                            resource_id=rid,
                            charge_period_start=day,
                            service_name=SERVICE,
                            sku_meter=_usage_type(words, direction),
                            consumed_quantity=qty,
                            billed_cost=(qty * Decimal(price)).quantize(_MONEY),
                            billing_currency="USD",
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
                        cloud="aws",
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
        return []  # The demo has no Bedrock provisioned throughput (AWS reports no utilisation).


def demo_aws_price_points() -> dict[str, Decimal]:
    """USD list price per 1M tokens per region-stripped usage type, for ``pricing.demo_aws``."""
    points: dict[str, Decimal] = {}
    for _name, _ver, words, _tin, _tout, pin, pout, _tags in _DEPLOYMENTS:
        points[_usage_type(words, "input")] = Decimal(pin)
        points[_usage_type(words, "output")] = Decimal(pout)
    return points
