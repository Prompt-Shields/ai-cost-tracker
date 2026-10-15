"""Cost Management Query API: request body, date chunking and response parsing.

We use the Query API rather than exports so connecting a subscription needs only ``Reader``
and nothing created in Azure (ARCHITECTURE.md D3, §4.3).

Unverified against a live pay-as-you-go subscription; the phase 0 spike (issue #2, §4.5)
confirms:
- the cost column name. EA / MCA scopes use ``Cost``; legacy pay-as-you-go (MOSP) scopes are
  documented to use ``PreTaxCost`` and may reject ``Cost`` in the aggregation. The parser
  accepts either and the connector retries once with ``PreTaxCost`` on a 400;
- that the Query API allows at most two grouping dimensions, which is why tags come from
  inventory instead of cost rows;
- that the ``ResourceType`` filter matches lower-case type names;
- the range limit. Ranges over one year are rejected; we query one calendar month at a time,
  which also keeps each response small;
- that ``UsageDate`` comes back as a ``yyyymmdd`` number for daily granularity (strings in
  ISO form are accepted too).
"""

from calendar import monthrange
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

DEFAULT_COST_COLUMN = "Cost"
PAYG_COST_COLUMN = "PreTaxCost"

_COST_COLUMNS = ("Cost", "PreTaxCost", "CostUSD", "totalCost")
_QTY_COLUMNS = ("UsageQuantity", "totalQuantity")


def query_url(subscription_id: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query"
        f"?api-version={API_VERSION}"
    )


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split inclusive ``start``..``end`` into inclusive calendar-month pieces."""
    chunks = []
    cursor = start
    while cursor <= end:
        month_end = cursor.replace(day=monthrange(cursor.year, cursor.month)[1])
        chunks.append((cursor, min(month_end, end)))
        cursor = date.fromordinal(month_end.toordinal() + 1)
    return chunks


def build_query(start: date, end: date, cost_column: str = DEFAULT_COST_COLUMN) -> dict[str, Any]:
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
                "totalCost": {"name": cost_column, "function": "Sum"},
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
    """Turn one page of a Query API response into cost records.

    Columns are located by name, never by position: their order depends on the request and
    is not documented as stable.
    """
    props = payload.get("properties") or {}
    names = [c["name"] for c in props.get("columns") or []]
    idx = {n: i for i, n in enumerate(names)}
    cost_i = _first(idx, _COST_COLUMNS)
    qty_i = _first(idx, _QTY_COLUMNS)
    if cost_i is None or "UsageDate" not in idx:
        raise ValueError(f"unexpected Cost Management columns: {names}")

    records = []
    for row in props.get("rows") or []:
        resource_id = row[idx["ResourceId"]] if "ResourceId" in idx else None
        qty = row[qty_i] if qty_i is not None else None
        records.append(
            CostRecord(
                cloud="azure",
                sub_account_id=subscription_id,
                resource_id=resource_id or None,
                charge_period_start=_usage_date(row[idx["UsageDate"]]),
                sku_meter=str(row[idx["Meter"]] or "unknown") if "Meter" in idx else "unknown",
                consumed_quantity=Decimal(str(qty)) if qty is not None else None,
                billed_cost=Decimal(str(row[cost_i] or 0)),
                billing_currency=str(row[idx["Currency"]]) if "Currency" in idx else "USD",
            )
        )
    return records


def next_link(payload: dict[str, Any]) -> str | None:
    """Absolute URL of the next page (it carries a ``$skiptoken``); POST the same body to it."""
    return (payload.get("properties") or {}).get("nextLink") or None


def _first(idx: dict[str, int], candidates: tuple[str, ...]) -> int | None:
    return next((idx[c] for c in candidates if c in idx), None)


def _usage_date(value: Any) -> date:
    # Daily granularity returns UsageDate as a number like 20261012 (sometimes as a float).
    text = str(int(value)) if isinstance(value, int | float) else str(value)
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
