"""PTU utilisation and idle-capacity report (issue #32); maths in ``ai_cost_tracker.ptu``.

A deployment is addressed by ``<account resource name>/<deployment>`` (the ``deployment_key``
field of the list). The slash may be sent raw or percent-encoded.
"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ai_cost_tracker import ptu
from ai_cost_tracker.api.auth import get_principal
from ai_cost_tracker.api.deps import Range, SessionDep, TenantId, money

router = APIRouter(tags=["ptu"], dependencies=[Depends(get_principal)])


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


@router.get("/ptu")
async def ptu_list(rng: Range, session: SessionDep, tenant_id: TenantId) -> dict[str, Any]:
    """Every PTU deployment with utilisation, idle hours, PTU cost and the idle share of it.

    Utilisation values are 0..1 strings; ``hours`` counts hours with data. Reservation
    purchases (heuristic, see ``ai_cost_tracker.ptu``) are listed apart from PTU cost.
    """
    report = await ptu.ptu_report(session, tenant_id, rng.start, rng.end)
    return {
        "currency": report.currency,
        "idle_threshold": format(ptu.IDLE_THRESHOLD, "f"),
        "deployments": [
            {
                "deployment_key": ptu.deployment_key(d.resource_id, d.deployment),
                "resource_id": d.resource_id,
                "account_id": d.account_id,
                "deployment": d.deployment,
                "model": d.model,
                "sku": d.sku,
                "capacity": d.capacity,
                "avg_utilization": _ratio(d.avg_utilization),
                "peak_utilization": _ratio(d.peak_utilization),
                "idle_hours": d.idle_hours,
                "hours": d.hours,
                "ptu_cost": money(d.ptu_cost),
                "idle_cost": None if d.idle_cost is None else money(d.idle_cost),
                "currency": d.currency,
            }
            for d in report.deployments
        ],
        "reservations": [
            {
                "day": r.day.isoformat(),
                "account_id": r.account_id,
                "resource_id": r.resource_id,
                "sku_meter": r.sku_meter,
                "service_name": r.service_name,
                "cost": money(r.cost),
                "currency": r.currency,
            }
            for r in report.reservations
        ],
    }


@router.get("/ptu/{deployment_key:path}/hourly")
async def ptu_hourly(
    deployment_key: str, rng: Range, session: SessionDep, tenant_id: TenantId
) -> dict[str, Any]:
    """Hourly utilisation (0..1) of one PTU deployment, oldest first."""
    deps = await ptu.ptu_deployments(session, tenant_id, rng.start, rng.end)
    matches = sorted(k for k in deps if ptu.deployment_key(*k) == deployment_key)
    if not matches:
        raise HTTPException(404, "unknown PTU deployment")
    if len(matches) > 1:
        raise HTTPException(409, "several PTU deployments share this key")
    resource_id, deployment = matches[0]
    rows = await ptu.ptu_hourly(session, tenant_id, resource_id, deployment, rng.start, rng.end)
    return {
        "resource_id": resource_id,
        "deployment": deployment,
        "rows": [{"hour": h.isoformat(), "utilization": format(v, "f")} for h, v in rows],
    }
