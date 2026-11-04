"""Cost and token aggregates over a day range (issue #22).

Billed cost comes from cost lines, estimated cost from token x price estimates; see
``api.queries`` for how a day is judged billed and how cost reaches deployments and tags.
All money is in ``settings.reporting_currency``; ``fx_approximate`` flags stale/missing rates.
"""

from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from ai_cost_tracker.api import queries as q
from ai_cost_tracker.api.auth import get_principal
from ai_cost_tracker.api.deps import (
    Cloud,
    CostFilters,
    Range,
    SessionDep,
    SettingsDep,
    TenantId,
    Today,
    money,
)
from ai_cost_tracker.db.models import TokenUsageRow

router = APIRouter(tags=["costs"], dependencies=[Depends(get_principal)])

GroupBy = Annotated[
    str,
    Query(
        description="model | deployment (<resource>/<deployment>) | account | cloud | "
        'tag:<key> (rows without the tag go to "untagged")',
        pattern=r"^(model|deployment|account|cloud|tag:.+)$",
    ),
]


def _grouper(group_by: str, inventory: q.Inventory) -> Callable[[q.CellKey], str]:
    if group_by == "model":
        return lambda k: k.model
    if group_by == "account":
        return lambda k: k.account_id
    if group_by == "cloud":
        return lambda k: k.cloud
    if group_by == "deployment":
        return lambda k: q.deployment_label(k.resource_id, k.deployment)
    tag_key = group_by.removeprefix("tag:")
    return lambda k: str(inventory.tags(k.resource_id, k.deployment).get(tag_key) or q.UNTAGGED)


@router.get("/costs/daily")
async def costs_daily(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    settings: SettingsDep,
    group_by: GroupBy = "model",
) -> dict[str, Any]:
    """Billed and estimated cost per day and group. Show ``estimated_cost`` for rows whose
    ``day_status`` is "estimated" (billing not in yet for every account in the row)."""
    inventory = await q.load_inventory(session, tenant_id)
    ccy = settings.reporting_currency
    data = await q.load(session, tenant_id, rng.start, rng.end, filters, inventory, ccy)
    key_of = _grouper(group_by, inventory)
    rows: dict[tuple[date, str], dict[str, Any]] = {}
    for key, cell in data.cells.items():
        if not cell.has_cost:
            continue
        row = rows.setdefault(
            (key.day, key_of(key)),
            {"billed": Decimal(0), "estimated": Decimal(0), "statuses": set()},
        )
        row["billed"] += cell.billed
        row["estimated"] += cell.estimated
        row["statuses"].add(data.day_status(key.day, key.account_id))
    out = []
    for (day, key), row in sorted(rows.items()):
        item: dict[str, Any] = {"day": day.isoformat(), "key": key}
        if group_by == "model":
            item["model"] = key  # M0 field, kept for existing clients
        item |= {
            "billed_cost": money(row["billed"]),
            "estimated_cost": money(row["estimated"]),
            "day_status": q.min_status(row["statuses"]),
        }
        out.append(item)
    return {"currency": data.currency, "fx_approximate": data.fx_approximate, "rows": out}


@router.get("/usage/daily")
async def usage_daily(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    cloud: Cloud = None,
) -> dict[str, Any]:
    day = q.usage_day(session)
    lo, hi = q.usage_window(rng.start, rng.end)
    result = await session.execute(
        select(
            day,
            TokenUsageRow.deployment,
            TokenUsageRow.model,
            TokenUsageRow.cloud,
            func.sum(TokenUsageRow.input_tokens),
            func.sum(TokenUsageRow.output_tokens),
            func.sum(TokenUsageRow.requests),
        )
        .where(
            TokenUsageRow.tenant_id == tenant_id,
            TokenUsageRow.bucket_start >= lo,
            TokenUsageRow.bucket_start < hi,
            *q.cloud_filter(TokenUsageRow.cloud, cloud),
        )
        .group_by(day, TokenUsageRow.deployment, TokenUsageRow.model, TokenUsageRow.cloud)
        .order_by(day, TokenUsageRow.deployment, TokenUsageRow.model, TokenUsageRow.cloud)
    )
    return {
        "rows": [
            {
                "day": q.as_date(d).isoformat(),
                "cloud": c,
                "deployment": dep,
                "model": m,
                "input_tokens": int(inp or 0),
                "output_tokens": int(out or 0),
                "requests": int(req or 0),
            }
            for d, dep, m, c, inp, out, req in result
        ]
    }


def _total(data: q.Dataset) -> Decimal:
    return sum((data.effective(k, c)[0] for k, c in data.cells.items()), Decimal(0))


@router.get("/summary")
async def summary(
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    today: Today,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Totals for the range, plus today's estimate and month to date (both relative to today,
    not the range). Costs are billed where billing is in and estimated elsewhere."""
    inventory = await q.load_inventory(session, tenant_id)
    ccy = settings.reporting_currency
    data = await q.load(session, tenant_id, rng.start, rng.end, filters, inventory, ccy)
    today_data = await q.load(session, tenant_id, today, today, filters, inventory, ccy)
    mtd = await q.load(session, tenant_id, today.replace(day=1), today, filters, inventory, ccy)

    models: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"has_cost": False, "num": [Decimal(0)] * 5, "tok": [0, 0]}
    )
    for key, cell in data.cells.items():
        m = models[key.model]
        m["has_cost"] |= cell.has_cost
        eff, eff_in, eff_out = data.effective(key, cell)
        for i, v in enumerate((cell.billed, cell.estimated, eff, eff_in, eff_out)):
            m["num"][i] += v
        m["tok"][0] += cell.input_tokens
        m["tok"][1] += cell.output_tokens

    # Models with tokens but no cost line and no estimate are left out rather than shown free.
    ranked = sorted(
        ((name, m) for name, m in models.items() if m["has_cost"]),
        key=lambda item: (-item[1]["num"][2], item[0]),
    )
    by_model = []
    for name, m in ranked:
        billed, estimated, eff, eff_in, eff_out = m["num"]
        inp, out = m["tok"]
        by_model.append(
            {
                "model": name,
                "billed_cost": money(billed),
                "estimated_cost": money(estimated),
                "input_tokens": inp,
                "output_tokens": out,
                "cost_per_1m_tokens": q.per_1m(eff, inp + out),
                "cost_per_1m_input": q.per_1m(eff_in, inp),
                "cost_per_1m_output": q.per_1m(eff_out, out),
            }
        )
    return {
        "currency": data.currency,
        "fx_approximate": any(d.fx_approximate for d in (data, today_data, mtd)),
        "total_cost": money(_total(data)),
        "today_estimated_cost": money(sum((c.estimated for c in today_data.cells.values()), 0)),
        "month_to_date_cost": money(_total(mtd)),
        "by_model": by_model,
        "by_cloud": _by_cloud(data),
    }


def _by_cloud(data: q.Dataset) -> list[dict[str, Any]]:
    """Each cloud's billed, estimated and effective total (same rule as ``total_cost``)."""
    clouds: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"num": [Decimal(0)] * 3, "tok": [0, 0]}
    )
    for key, cell in data.cells.items():
        c = clouds[key.cloud]
        for i, v in enumerate((cell.billed, cell.estimated, data.effective(key, cell)[0])):
            c["num"][i] += v
        c["tok"][0] += cell.input_tokens
        c["tok"][1] += cell.output_tokens
    ranked = sorted(clouds.items(), key=lambda item: (-item[1]["num"][2], item[0]))
    return [
        {
            "cloud": cloud,
            "billed_cost": money(c["num"][0]),
            "estimated_cost": money(c["num"][1]),
            "total_cost": money(c["num"][2]),
            "input_tokens": c["tok"][0],
            "output_tokens": c["tok"][1],
        }
        for cloud, c in ranked
    ]
