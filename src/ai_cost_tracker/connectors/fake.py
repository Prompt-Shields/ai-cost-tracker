"""Deterministic demo data, so the app runs locally without an Azure subscription."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ai_cost_tracker.core.models import CloudAccount, CostRecord, Deployment, TokenUsage

_SUB = "00000000-0000-0000-0000-00000000demo"
_RG = f"/subscriptions/{_SUB}/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts"

# (deployment, account, model, version, hourly input tokens, hourly output tokens,
#  $ per 1M input, $ per 1M output, team tag)
_DEPLOYMENTS = [
    (
        "search-rag-gpt-4o",
        "oai-search",
        "gpt-4o",
        "2024-08-06",
        400_000,
        60_000,
        "2.50",
        "10.00",
        "search",
    ),
    (
        "support-bot-gpt-4o-mini",
        "oai-support",
        "gpt-4o-mini",
        "2024-07-18",
        900_000,
        150_000,
        "0.15",
        "0.60",
        "support",
    ),
    (
        "analytics-o3-mini",
        "oai-analytics",
        "o3-mini",
        "2025-01-31",
        50_000,
        120_000,
        "1.10",
        "4.40",
        "analytics",
    ),
]


class FakeConnector:
    name = "fake"

    def __init__(self, today: date | None = None) -> None:
        self._fixed_today = today

    def _today(self) -> date:
        return getattr(self, "_fixed_today", None) or datetime.now(UTC).date()

    async def list_accounts(self, include_all: bool = False) -> list[CloudAccount]:
        return [
            CloudAccount(
                cloud="azure", account_id=_SUB, name="Demo subscription", has_ai_resources=True
            )
        ]

    async def check_access(self, account_id: str) -> bool:
        return True

    async def list_deployments(self, account_id: str) -> list[Deployment]:
        return [
            Deployment(
                cloud="azure",
                account_id=account_id,
                resource_id=f"{_RG}/{acct}",
                name=name,
                model=model,
                model_version=version,
                sku="GlobalStandard",
                region="eastus",
                tags={"team": team},
            )
            for name, acct, model, version, *_rest, team in _DEPLOYMENTS
        ]

    async def pull_cost(self, account_id: str, start: date, end: date) -> list[CostRecord]:
        # Like real billing, today isn't billed yet: it shows as an estimate until tomorrow.
        end = min(end, self._today() - timedelta(days=1))
        records: list[CostRecord] = []
        day = start
        while day <= end:
            for name, acct, model, version, tin, tout, pin, pout, _team in _DEPLOYMENTS:
                factor = _day_factor(day, name)
                for direction, tokens, price in (("Inp", tin, pin), ("Outp", tout, pout)):
                    qty = Decimal(tokens * 24) * factor / Decimal(1_000_000)
                    records.append(
                        CostRecord(
                            cloud="azure",
                            sub_account_id=account_id,
                            resource_id=f"{_RG}/{acct}",
                            charge_period_start=day,
                            service_name="Foundry Models",
                            sku_meter=f"{model}-{version[5:].replace('-', '')} {direction} glbl",
                            consumed_quantity=qty.quantize(Decimal("0.000001")),
                            billed_cost=(qty * Decimal(price)).quantize(Decimal("0.0000000001")),
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
                factor = _day_factor(hour.date(), dep.name)
                out.append(
                    TokenUsage(
                        cloud="azure",
                        account_id=account_id,
                        resource_id=dep.resource_id,
                        deployment=dep.name,
                        model=dep.model,
                        bucket_start=hour,
                        input_tokens=int(spec[4] * factor),
                        output_tokens=int(spec[5] * factor),
                        requests=int(spec[4] * factor) // 2_000,
                    )
                )
            hour += timedelta(hours=1)
        return out


def _day_factor(day: date, seed: str) -> Decimal:
    """Repeatable day-to-day variation between 0.7 and 1.3, lower at weekends."""
    wobble = ((day.toordinal() * 7919 + sum(map(ord, seed))) % 600) / 1000
    weekend = Decimal("0.5") if day.weekday() >= 5 else Decimal(1)
    return (Decimal("0.7") + Decimal(str(wobble))) * weekend


def demo_price_points() -> list[dict]:
    """Per-1M-token list prices matching the demo meters, for ``pricing.demo``."""
    points = []
    for _name, _acct, model, version, _tin, _tout, pin, pout, _team in _DEPLOYMENTS:
        for direction, short, price in (("input", "Inp", pin), ("output", "Outp", pout)):
            points.append(
                {
                    "sku_meter": f"{model}-{version[5:].replace('-', '')} {short} glbl",
                    "unit_price": Decimal(price),
                    "ai_model": model,
                    "ai_direction": direction,
                }
            )
    return points
