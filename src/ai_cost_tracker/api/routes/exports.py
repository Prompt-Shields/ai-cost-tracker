"""CSV exports of the table views (issue #35).

Each export calls the same handler (or aggregation) as its JSON endpoint with the same query
parameters, so the file always matches what the screen shows. Money stays a plain decimal
string, exactly as the JSON carries it.
"""

import csv
import io
import json
import uuid
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ai_cost_tracker import audit
from ai_cost_tracker.api import queries as q
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.api.deps import CostFilters, Range, SessionDep, TenantId, Today, money
from ai_cost_tracker.api.routes import costs, inventory

router = APIRouter(tags=["exports"])

Caller = Annotated[Principal, Depends(get_principal)]


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buf.getvalue()


async def _respond(
    request: Request,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Principal,
    name: str,
    body: str,
    rng: Any = None,
) -> Response:
    """Audit the export, then send it as a download."""
    audit.record(
        session,
        tenant_id,
        principal,
        "export.csv",
        target_type="export",
        target_id=name,
        details={"export": name, "params": dict(request.query_params)},
    )
    await session.commit()
    suffix = f"_{rng.start.isoformat()}_{rng.end.isoformat()}" if rng is not None else ""
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}{suffix}.csv"'},
    )


@router.get("/exports/costs-daily.csv")
async def costs_daily_csv(
    request: Request,
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    principal: Caller,
    group_by: costs.GroupBy = "model",
) -> Response:
    data = await costs.costs_daily(
        rng=rng, session=session, tenant_id=tenant_id, filters=filters, group_by=group_by
    )
    rows = [r | {"currency": data["currency"]} for r in data["rows"]]
    cols = ["day", "key", "billed_cost", "estimated_cost", "day_status", "currency"]
    return await _respond(
        request, session, tenant_id, principal, "costs-daily", _csv(cols, rows), rng
    )


@router.get("/exports/usage-daily.csv")
async def usage_daily_csv(
    request: Request, rng: Range, session: SessionDep, tenant_id: TenantId, principal: Caller
) -> Response:
    data = await costs.usage_daily(rng=rng, session=session, tenant_id=tenant_id)
    cols = ["day", "deployment", "model", "input_tokens", "output_tokens", "requests"]
    return await _respond(
        request, session, tenant_id, principal, "usage-daily", _csv(cols, data["rows"]), rng
    )


@router.get("/exports/deployment-daily.csv")
async def deployment_daily_csv(
    request: Request,
    deployment_id: uuid.UUID,
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    principal: Caller,
) -> Response:
    """The deployment page's daily table (``/deployments/{id}/daily``)."""
    data = await inventory.deployment_daily(
        deployment_id=deployment_id, rng=rng, session=session, tenant_id=tenant_id
    )
    cols = [
        "day",
        "billed_cost",
        "estimated_cost",
        "input_tokens",
        "output_tokens",
        "cost_per_1m_tokens",
        "day_status",
    ]
    return await _respond(
        request, session, tenant_id, principal, "deployment-daily", _csv(cols, data["rows"]), rng
    )


@router.get("/exports/deployments.csv")
async def deployments_csv(
    request: Request, session: SessionDep, tenant_id: TenantId, principal: Caller
) -> Response:
    rows = await inventory.list_deployments(session=session, tenant_id=tenant_id)
    cols = [
        "id",
        "cloud",
        "account_id",
        "account_name",
        "resource_id",
        "name",
        "model",
        "model_version",
        "sku",
        "capacity",
        "region",
        "tags",
    ]
    return await _respond(request, session, tenant_id, principal, "deployments", _csv(cols, rows))


@router.get("/exports/summary.csv")
async def summary_csv(
    request: Request,
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    today: Today,
    principal: Caller,
) -> Response:
    """The by-model table; the range totals are repeated on every row for spreadsheets."""
    data = await costs.summary(
        rng=rng, session=session, tenant_id=tenant_id, filters=filters, today=today
    )
    totals = {
        k: data[k] for k in ("currency", "total_cost", "today_estimated_cost", "month_to_date_cost")
    }
    rows = [m | totals for m in data["by_model"]]
    cols = [
        "model",
        "billed_cost",
        "estimated_cost",
        "input_tokens",
        "output_tokens",
        "cost_per_1m_tokens",
        "cost_per_1m_input",
        "cost_per_1m_output",
        "currency",
        "total_cost",
        "today_estimated_cost",
        "month_to_date_cost",
    ]
    return await _respond(request, session, tenant_id, principal, "summary", _csv(cols, rows), rng)


async def _cost_centres_daily(
    session: SessionDep, tenant_id: TenantId, rng: Any, filters: q.Filters, level: str
) -> dict[str, Any]:
    """The rows of ``GET /allocation/cost-centres/daily``, from the same aggregation."""
    try:
        # Imported here: allocation is built in parallel with this module (issue #33).
        from ai_cost_tracker.allocation import cost_centres_daily  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(501, "cost-centre allocation is not available yet") from exc
    rows = await cost_centres_daily(session, tenant_id, rng.start, rng.end, filters, level)
    return {
        "currency": await q.currency(session, tenant_id, rng.start, rng.end),
        "rows": [
            {
                "day": r.day.isoformat(),
                "key": r.key,
                "cost": money(r.cost),
                "approximate": r.approximate,
            }
            for r in rows
        ],
    }


@router.get("/exports/cost-centres-daily.csv")
async def cost_centres_daily_csv(
    request: Request,
    rng: Range,
    session: SessionDep,
    tenant_id: TenantId,
    filters: CostFilters,
    principal: Caller,
    level: Annotated[Literal["cost_centre", "team", "app"], Query()] = "cost_centre",
) -> Response:
    data = await _cost_centres_daily(session, tenant_id, rng, filters, level)
    rows = [r | {"currency": data["currency"]} for r in data["rows"]]
    cols = ["day", "key", "cost", "approximate", "currency"]
    return await _respond(
        request, session, tenant_id, principal, "cost-centres-daily", _csv(cols, rows), rng
    )
