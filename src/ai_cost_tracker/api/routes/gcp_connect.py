""" "Connect Google Cloud" endpoints (ARCHITECTURE.md §8, issue #46).

Unlike Azure, the app can't grant itself access: a project owner runs the ``gcloud``
commands we hand out. Adding a project checks access right away; if the roles aren't there
yet it is stored ``waiting_for_access`` and the worker's access check connects it (and starts
the backfill) as soon as they appear.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, SettingsDep, TenantId
from ai_cost_tracker.api.routes import connections
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors import get_connector_for
from ai_cost_tracker.connectors.base import Connector
from ai_cost_tracker.connectors.gcp import onboarding
from ai_cost_tracker.db.models import CloudAccountRow
from ai_cost_tracker.ingest import get_or_create_tenant

CLOUD = "gcp"
CONNECTED = "connected"
WAITING = "waiting_for_access"
DOCS_PATH = "docs/gcp-setup.md"

router = APIRouter(tags=["gcp"], dependencies=[Depends(get_principal)])
Admin = Annotated[Principal, Depends(require_role("admin"))]


async def get_gcp_connector(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[Connector]:
    """The GCP connector for this request (fake in demo mode); closed afterwards.

    503 when GCP isn't enabled or the real connector lacks settings, so the UI can point at
    the one-time setup instead of showing a stack trace. Tests override this dependency.
    """
    if settings.connector == "fake":
        connector = get_connector_for(settings, CLOUD)
    else:
        missing = onboarding.missing_settings(settings)
        if "ACT_GCP_ENABLED" in missing:
            raise HTTPException(503, "GCP is not enabled (set ACT_GCP_ENABLED=true)")
        # Projects and access checks work without the billing table; only cost needs it.
        needed = [m for m in missing if m != "ACT_GCP_BILLING_EXPORT_TABLE"]
        if needed:
            raise HTTPException(
                503, f"GCP is not configured yet: set {', '.join(needed)} (see {DOCS_PATH})"
            )
        # Built directly rather than via get_connector_for, which would also construct the
        # Azure connector (and its credential) just to throw it away.
        from ai_cost_tracker.connectors.gcp import GcpConnector

        connector = GcpConnector.from_settings(settings)
    try:
        yield connector
    finally:
        close = getattr(connector, "aclose", None)
        if close is not None:
            await close()


GcpConnectorDep = Annotated[Connector, Depends(get_gcp_connector)]


class AddProjectBody(BaseModel):
    project_id: str

    @field_validator("project_id")
    @classmethod
    def _valid(cls, value: str) -> str:
        return onboarding.validate_project_id(value.strip())


@router.get("/gcp/setup")
async def setup(settings: SettingsDep) -> dict[str, Any]:
    """Everything the Connect page needs before any project is added. Any signed-in user."""
    fake = settings.connector == "fake"
    missing = [] if fake else onboarding.missing_settings(settings)
    sa = _service_account(settings)
    table = settings.gcp_billing_export_table or None
    return {
        "enabled": fake or settings.gcp_enabled,
        "configured": not missing,
        "missing": missing,
        "service_account": sa,
        "billing_export_table": table,
        "roles": [{"role": r, "scope": s, "why": w} for r, s, w in onboarding.ROLES],
        "setup_script": onboarding.setup_script(settings),
        "billing_grant_commands": _billing_commands(table, sa),
        "docs_path": DOCS_PATH,
    }


@router.get("/gcp/projects/visible")
async def visible_projects(
    _: Admin, connector: GcpConnectorDep, session: SessionDep, tenant_id: TenantId
) -> list[dict[str, Any]]:
    """Projects the service account can see, merged with the ones already stored."""
    accounts = await _call_google(connector.list_accounts(include_all=True))
    stored = await _accounts(session, tenant_id)
    out = []
    for a in accounts:
        row = stored.pop(a.account_id, None)
        out.append(
            {
                "project_id": a.account_id,
                "name": a.name,
                "has_ai_resources": a.has_ai_resources,
                "tracked": row.tracked if row else False,
                "access_status": row.access_status if row else None,
            }
        )
    # Projects added by id that the service account can't list yet (typically waiting).
    for row in stored.values():
        out.append(
            {
                "project_id": row.account_id,
                "name": row.name,
                "has_ai_resources": row.has_ai_resources,
                "tracked": row.tracked,
                "access_status": row.access_status,
            }
        )
    return sorted(out, key=lambda p: (not p["has_ai_resources"], p["name"].lower()))


@router.post("/gcp/projects", status_code=201)
async def add_project(
    body: AddProjectBody,
    admin: Admin,
    connector: GcpConnectorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Track a project: connected now if the roles are in place, else waiting for them."""
    ok = await _call_google(connector.check_access(body.project_id))
    tenant_id = await get_or_create_tenant(session, settings.default_tenant_slug)
    row = (await _accounts(session, tenant_id, body.project_id)).get(body.project_id)
    was = row.access_status if row is not None else None
    if row is None:
        row = CloudAccountRow(
            tenant_id=tenant_id,
            cloud=CLOUD,
            account_id=body.project_id,
            name=body.project_id,
            discovered_at=datetime.now(UTC),
        )
        session.add(row)
    was_tracked = row.tracked if was is not None else False
    row.tracked = True
    row.access_status = CONNECTED if ok else WAITING
    audit.record(
        session,
        tenant_id,
        admin,
        "connection.connect",
        target_type="gcp_project",
        target_id=body.project_id,
        details={"cloud": CLOUD, "access_status": row.access_status, "previous": was},
    )
    await session.commit()
    if ok and not (was == CONNECTED and was_tracked):
        await connections.enqueue_backfill(body.project_id, CLOUD)
    return _project_result(row, settings)


@router.get("/gcp/projects/{project_id}/grant")
async def grant(project_id: str, settings: SettingsDep) -> dict[str, Any]:
    """Commands to send the project owner. Any signed-in user, so the link can be shared."""
    _validate(project_id)
    sa = _require_service_account(settings)
    return {
        "project_id": project_id,
        "service_account": sa,
        "commands": onboarding.project_grant_commands(project_id, sa),
    }


@router.post("/gcp/projects/{project_id}/check")
async def check_project(
    project_id: str,
    admin: Admin,
    connector: GcpConnectorDep,
    session: SessionDep,
    tenant_id: TenantId,
    settings: SettingsDep,
) -> dict[str, Any]:
    """ "Check again" after the owner ran the commands; same shape as adding the project."""
    _validate(project_id)
    row = (await _accounts(session, tenant_id, project_id)).get(project_id)
    if row is None:
        raise HTTPException(404, f"Project {project_id} has not been added")
    ok = await _call_google(connector.check_access(project_id))
    was = row.access_status
    if ok:
        row.access_status = CONNECTED
    elif was == CONNECTED:
        row.access_status = "no_access"
    audit.record(
        session,
        tenant_id,
        admin,
        "connection.check",
        target_type="gcp_project",
        target_id=project_id,
        details={"cloud": CLOUD, "access_status": row.access_status, "previous": was},
    )
    await session.commit()
    if ok and was != CONNECTED and row.tracked:
        await connections.enqueue_backfill(project_id, CLOUD)
    return _project_result(row, settings)


def _project_result(row: CloudAccountRow, settings: Settings) -> dict[str, Any]:
    return {
        "project_id": row.account_id,
        "name": row.name,
        "access_status": row.access_status,
        "grant_commands": onboarding.project_grant_commands(
            row.account_id, _service_account(settings) or onboarding.SERVICE_ACCOUNT_PLACEHOLDER
        ),
    }


def _service_account(settings: Settings) -> str | None:
    if settings.gcp_service_account:
        return settings.gcp_service_account
    return onboarding.DEMO_SERVICE_ACCOUNT if settings.connector == "fake" else None


def _require_service_account(settings: Settings) -> str:
    sa = _service_account(settings)
    if sa is None:
        raise HTTPException(
            503, f"ACT_GCP_SERVICE_ACCOUNT is not set: run the one-time setup ({DOCS_PATH})"
        )
    return sa


def _billing_commands(table: str | None, sa: str | None) -> str | None:
    if not table:
        return None
    try:
        return onboarding.billing_grant_commands(
            table, sa or onboarding.SERVICE_ACCOUNT_PLACEHOLDER
        )
    except ValueError:
        return None  # Malformed table setting; the billing pass reports it.


def _validate(project_id: str) -> None:
    try:
        onboarding.validate_project_id(project_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def _call_google(awaitable: Any) -> Any:
    """Await a connector call; Google/WIF failures become a 502 that says what failed.

    Connector errors never carry tokens (``connectors/gcp/auth.py`` scrubs them).
    """
    try:
        return await awaitable
    except HTTPException:
        raise
    except Exception as exc:
        first = (str(exc).strip().splitlines() or [""])[0][:300]
        raise HTTPException(
            502, f"Google Cloud call failed ({type(exc).__name__}): {first}"
        ) from None


async def _accounts(
    session: AsyncSession, tenant_id: uuid.UUID, project_id: str | None = None
) -> dict[str, CloudAccountRow]:
    query = select(CloudAccountRow).where(
        CloudAccountRow.tenant_id == tenant_id, CloudAccountRow.cloud == CLOUD
    )
    if project_id is not None:
        query = query.where(CloudAccountRow.account_id == project_id)
    return {r.account_id: r for r in await session.scalars(query)}
