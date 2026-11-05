""" "Connect AWS" endpoints (ARCHITECTURE.md §8, phase 4, issue #52).

Like GCP, the app can't grant itself access: someone signed in to each AWS account deploys
our CloudFormation stack (a read-only role trusted through Entra OIDC, no keys). Adding an
account checks access right away; if the role isn't there yet the account is stored
``waiting_for_access`` and the worker's access check connects it (and starts the backfill)
as soon as the stack exists.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, SettingsDep, TenantId
from ai_cost_tracker.api.routes import connections
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.connectors import get_connector_for
from ai_cost_tracker.connectors.aws import onboarding
from ai_cost_tracker.connectors.base import Connector
from ai_cost_tracker.db.models import CloudAccountRow
from ai_cost_tracker.ingest import get_or_create_tenant

CLOUD = "aws"
CONNECTED = "connected"
WAITING = "waiting_for_access"
DOCS_PATH = "docs/aws-setup.md"

router = APIRouter(tags=["aws"], dependencies=[Depends(get_principal)])
Admin = Annotated[Principal, Depends(require_role("admin"))]


async def get_aws_connector(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AsyncIterator[Connector]:
    """The AWS connector for this request (fake in demo mode); closed afterwards.

    503 when AWS isn't enabled, so the UI can point at the one-time setup instead of a stack
    trace. Access checks and account listing need nothing else; the export settings only
    matter for cost. Tests override this dependency.
    """
    if settings.connector == "fake":
        connector = get_connector_for(settings, CLOUD)
    else:
        if not settings.aws_enabled:
            raise HTTPException(
                503, f"AWS is not enabled (set ACT_AWS_ENABLED=true, see {DOCS_PATH})"
            )
        # Built directly rather than via get_connector_for, which would also construct the
        # Azure connector (and its credential) just to throw it away.
        from ai_cost_tracker.connectors.aws import AwsConnector

        connector = AwsConnector.from_settings(settings)
    try:
        yield connector
    finally:
        close = getattr(connector, "aclose", None)
        if close is not None:
            await close()


AwsConnectorDep = Annotated[Connector, Depends(get_aws_connector)]


class AddAccountBody(BaseModel):
    account_id: str

    @field_validator("account_id")
    @classmethod
    def _valid(cls, value: str) -> str:
        return onboarding.validate_account_id(value.strip().replace("-", ""))


@router.get("/aws/setup")
async def setup(settings: SettingsDep) -> dict[str, Any]:
    """Everything the Connect page needs before any account is added. Any signed-in user."""
    fake = settings.connector == "fake"
    missing = [] if fake else onboarding.missing_settings(settings)
    return {
        "enabled": fake or settings.aws_enabled,
        "configured": not missing,
        "missing": missing,
        "role_name": settings.aws_role_name,
        "export": _export(settings),
        "permissions": [{"permission": p, "why": w} for p, w in onboarding.PERMISSIONS],
        "template_url": settings.aws_cfn_template_url or None,
        "deploy_command_member": onboarding.deploy_command(settings, export_account=False),
        "deploy_command_export": onboarding.deploy_command(settings, export_account=True),
        "docs_path": DOCS_PATH,
    }


@router.get("/aws/template.yaml")
async def template() -> Response:
    """The CloudFormation template to deploy in each account. Any signed-in user."""
    try:
        text = onboarding.template_text()
    except OSError:
        raise HTTPException(
            503, "CloudFormation template not found in this build (infra/aws/readonly-role.yaml)"
        ) from None
    return Response(
        text,
        media_type="text/yaml",
        headers={"Content-Disposition": f'attachment; filename="{onboarding.TEMPLATE_FILENAME}"'},
    )


@router.get("/aws/accounts/visible")
async def visible_accounts(
    _: Admin, connector: AwsConnectorDep, session: SessionDep, tenant_id: TenantId
) -> list[dict[str, Any]]:
    """Accounts the app can see (Organizations via the export account), merged with stored."""
    accounts = await _call_aws(connector.list_accounts(include_all=True))
    stored = await _accounts(session, tenant_id)
    out = []
    for a in accounts:
        row = stored.pop(a.account_id, None)
        out.append(
            {
                "account_id": a.account_id,
                "name": a.name,
                "has_ai_resources": a.has_ai_resources,
                "tracked": row.tracked if row else False,
                "access_status": row.access_status if row else None,
            }
        )
    # Accounts added by id that the export account can't list (outside the organization).
    for row in stored.values():
        out.append(
            {
                "account_id": row.account_id,
                "name": row.name,
                "has_ai_resources": row.has_ai_resources,
                "tracked": row.tracked,
                "access_status": row.access_status,
            }
        )
    return sorted(out, key=lambda a: (not a["has_ai_resources"], a["name"].lower()))


@router.post("/aws/accounts", status_code=201)
async def add_account(
    body: AddAccountBody,
    admin: Admin,
    connector: AwsConnectorDep,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Track an account: connected now if the stack is deployed, else waiting for it."""
    ok = await _call_aws(connector.check_access(body.account_id))
    tenant_id = await get_or_create_tenant(session, settings.default_tenant_slug)
    row = (await _accounts(session, tenant_id, body.account_id)).get(body.account_id)
    was = row.access_status if row is not None else None
    if row is None:
        row = CloudAccountRow(
            tenant_id=tenant_id,
            cloud=CLOUD,
            account_id=body.account_id,
            name=body.account_id,
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
        target_type="aws_account",
        target_id=body.account_id,
        details={"cloud": CLOUD, "access_status": row.access_status, "previous": was},
    )
    await session.commit()
    if ok and not (was == CONNECTED and was_tracked):
        await connections.enqueue_backfill(body.account_id, CLOUD)
    return _account_result(row, settings)


@router.get("/aws/accounts/{account_id}/setup")
async def account_setup(account_id: str, settings: SettingsDep) -> dict[str, Any]:
    """What the account owner deploys. Any signed-in user, so the link can be shared."""
    _validate(account_id)
    export = onboarding.is_export_account(settings, account_id)
    return {
        "account_id": account_id,
        "role_arn": onboarding.account_role_arn(settings, account_id),
        "quick_create_url": onboarding.quick_create_url(
            settings, account_id, export_account=export
        ),
        "deploy_command": onboarding.deploy_command(settings, export_account=export),
    }


@router.post("/aws/accounts/{account_id}/check")
async def check_account(
    account_id: str,
    admin: Admin,
    connector: AwsConnectorDep,
    session: SessionDep,
    tenant_id: TenantId,
    settings: SettingsDep,
) -> dict[str, Any]:
    """ "Check again" after the owner deployed the stack; same shape as adding the account."""
    _validate(account_id)
    row = (await _accounts(session, tenant_id, account_id)).get(account_id)
    if row is None:
        raise HTTPException(404, f"Account {account_id} has not been added")
    ok = await _call_aws(connector.check_access(account_id))
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
        target_type="aws_account",
        target_id=account_id,
        details={"cloud": CLOUD, "access_status": row.access_status, "previous": was},
    )
    await session.commit()
    if ok and was != CONNECTED and row.tracked:
        await connections.enqueue_backfill(account_id, CLOUD)
    return _account_result(row, settings)


def _account_result(row: CloudAccountRow, settings: Settings) -> dict[str, Any]:
    export = onboarding.is_export_account(settings, row.account_id)
    return {
        "account_id": row.account_id,
        "name": row.name,
        "access_status": row.access_status,
        "quick_create_url": onboarding.quick_create_url(
            settings, row.account_id, export_account=export
        ),
        "deploy_command": onboarding.deploy_command(settings, export_account=export),
    }


def _export(settings: Settings) -> dict[str, str] | None:
    """Where the CUR 2.0 export lands; demo values in fake mode, None until configured."""
    account, bucket = settings.aws_export_account_id, settings.aws_export_bucket
    if settings.connector == "fake":
        account = account or onboarding.DEMO_EXPORT_ACCOUNT
        bucket = bucket or onboarding.DEMO_EXPORT_BUCKET
    if not (account and bucket):
        return None
    return {
        "account_id": account,
        "bucket": bucket,
        "prefix": settings.aws_export_prefix.strip("/"),
        "name": settings.aws_export_name,
        "region": settings.aws_export_region,
    }


def _validate(account_id: str) -> None:
    try:
        onboarding.validate_account_id(account_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def _call_aws(awaitable: Any) -> Any:
    """Await a connector call; AWS/OIDC failures become a 502 that says what failed.

    Connector errors never carry tokens or credentials (``connectors/aws/auth.py``).
    """
    try:
        return await awaitable
    except HTTPException:
        raise
    except Exception as exc:
        first = (str(exc).strip().splitlines() or [""])[0][:300]
        raise HTTPException(502, f"AWS call failed ({type(exc).__name__}): {first}") from None


async def _accounts(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: str | None = None
) -> dict[str, CloudAccountRow]:
    query = select(CloudAccountRow).where(
        CloudAccountRow.tenant_id == tenant_id, CloudAccountRow.cloud == CLOUD
    )
    if account_id is not None:
        query = query.where(CloudAccountRow.account_id == account_id)
    return {r.account_id: r for r in await session.scalars(query)}
