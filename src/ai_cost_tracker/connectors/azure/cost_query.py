"""Cost Management Query API: request body and response parsing.

We use the Query API rather than exports so connecting a subscription needs only ``Reader``
and nothing created in Azure (ARCHITECTURE.md D3, §4.3).

Unverified against a live pay-as-you-go subscription; the phase 0 spike (§4.5) confirms:
- the cost column name (``Cost`` for EA / MCA, ``PreTaxCost`` for pay-as-you-go), so the
  parser accepts either;
- that the Query API allows at most two grouping dimensions, which is why tags come from
  inventory instead of cost rows.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ai_cost_tracker.core.models import CostRecord

API_VERSION = "2023-11-01"

# Resource types whose cost we track: Azure OpenAI / AI Services accounts, and Foundry
# workspaces that bill serverless model endpoints.
AI_RESOURCE_TYPES = [
    "microsoft.cognitiveservices/accounts",
    "microsoft.machinelearningservices/workspaces",
]

_COST_COLUMNS = ("Cost", "PreTaxCost", "totalCost")
_QTY_COLUMNS = ("UsageQuantity", "totalQuantity")


def query_url(subscription_id: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query"
        f"?api-version={API_VERSION}"
    )


def build_query(start: date, end: date) -> dict[str, Any]:
    """Daily actual cost for AI resources in ``start``..``end``, by resource and meter."""
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": f"{start.isoformat()}T00:00:00Z",
            "to": f"{end.isoformat()}T23:59:59Z",
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
                "totalQuantity": {"name": "UsageQuantity", "function": "Sum"},
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "Meter"},
            ],
            "filter": {
                "dimensions": {
                    "name": "ResourceType",
                    "operator": "In",
                    "values": AI_RESOURCE_TYPES,
                }
            },
        },
    }


def parse_rows(subscription_id: str, payload: dict[str, Any]) -> list[CostRecord]:
    """Turn one page of a Query API response into cost records."""
    props = payload.get("properties", {})
    names = [c["name"] for c in props.get("columns", [])]
    idx = {n: i for i, n in enumerate(names)}
    cost_i = _first(idx, _COST_COLUMNS)
    qty_i = _first(idx, _QTY_COLUMNS)
    if cost_i is None or "UsageDate" not in idx:
        raise ValueError(f"unexpected Cost Management columns: {names}")

    records = []
    for row in props.get("rows", []):
        resource_id = row[idx["ResourceId"]] if "ResourceId" in idx else None
        records.append(
            CostRecord(
                cloud="azure",
                sub_account_id=subscription_id,
                resource_id=resource_id or None,
                charge_period_start=_usage_date(row[idx["UsageDate"]]),
                sku_meter=str(row[idx["Meter"]]) if "Meter" in idx else "unknown",
                consumed_quantity=Decimal(str(row[qty_i])) if qty_i is not None else None,
                billed_cost=Decimal(str(row[cost_i])),
                billing_currency=str(row[idx["Currency"]]) if "Currency" in idx else "USD",
            )
        )
    return records


def next_link(payload: dict[str, Any]) -> str | None:
    return payload.get("properties", {}).get("nextLink") or None


def _first(idx: dict[str, int], candidates: tuple[str, ...]) -> int | None:
    return next((idx[c] for c in candidates if c in idx), None)


def _usage_date(value: Any) -> date:
    # Daily granularity returns UsageDate as a number like 20261012.
    text = str(int(value)) if isinstance(value, int | float) else str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
