"""Connection health panel and the "new subscription found" banner (issues #24, #25).

Lists every cloud's accounts (Azure subscriptions, GCP projects, AWS accounts) with a ``cloud``
field.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker import audit
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, SettingsDep, TenantId
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.aws import onboarding as aws_onboarding
from ai_cost_tracker.connectors.gcp import onboarding as gcp_onboarding
from ai_cost_tracker.db.models import CloudAccountRow, SyncState
from ai_cost_tracker.health import describe

router = APIRouter(tags=["connections"], dependencies=[Depends(get_principal)])

HEALTH_SOURCES = ("inventory", "cost", "usage")
BACKFILL_SOURCES = ("cost", "usage")
Cloud = Literal["azure", "gcp", "aws"]


class TrackBody(BaseModel):
    tracked: bool


async def enqueue_backfill(account_id: str, cloud: str = "azure") -> None:
    """Queue a backfill. Imported lazily: worker.enqueue pulls in arq/Redis settings."""
    from ai_cost_tracker.worker.enqueue import enqueue_backfill as enqueue

    await enqueue(account_id, cloud)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat()


@router.get("/connections")
async def list_connections(
    session: SessionDep, tenant_id: TenantId, settings: SettingsDep
) -> list[dict[str, Any]]:
    accounts = (
        await session.scalars(
            select(CloudAccountRow)
            .where(CloudAccountRow.tenant_id == tenant_id)
            .order_by(CloudAccountRow.cloud, CloudAccountRow.name, CloudAccountRow.account_id)
        )
    ).all()
    states = await _sync_states(session, tenant_id)
    now = datetime.now(UTC)
    return [_connection(a, states, now, settings) for a in accounts]


@router.get("/connections/discovered")
async def list_discovered(session: SessionDep, tenant_id: TenantId) -> list[dict[str, Any]]:
    """Untracked accounts with AI resources: the "N new subscriptions found" banner."""
    rows = await session.scalars(
        select(CloudAccountRow)
        .where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.tracked.is_(False),
            CloudAccountRow.has_ai_resources.is_(True),
        )
        .order_by(CloudAccountRow.name, CloudAccountRow.account_id)
    )
    return [
        {
            "cloud": r.cloud,
            "account_id": r.account_id,
            "name": r.name,
            "discovered_at": utc_iso(r.discovered_at),
        }
        for r in rows
    ]


@router.post("/connections/{account_id}/track")
async def track_account(
    account_id: str,
    body: TrackBody,
    session: SessionDep,
    tenant_id: TenantId,
    settings: SettingsDep,
    admin: Annotated[Principal, Depends(require_role("admin"))],
    cloud: Cloud = "azure",
) -> dict[str, Any]:
    account = await session.scalar(
        select(CloudAccountRow).where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.cloud == cloud,
            CloudAccountRow.account_id == account_id,
        )
    )
    if account is None:
        raise HTTPException(404, f"Unknown account {account_id}")
    previous = account.tracked
    account.tracked = body.tracked
    audit.record(
        session,
        tenant_id,
        admin,
        "connection.track" if body.tracked else "connection.untrack",
        target_type="cloud_account",
        target_id=account.account_id,
        details={"cloud": account.cloud, "name": account.name, "was_tracked": previous},
    )
    await session.commit()
    if body.tracked and account.access_status == "connected":
        await enqueue_backfill(account.account_id, account.cloud)
    states = await _sync_states(session, tenant_id, account_id)
    return _connection(account, states, datetime.now(UTC), settings)


async def _sync_states(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: str | None = None
) -> dict[tuple[str, str, str], SyncState]:
    query = select(SyncState).where(SyncState.tenant_id == tenant_id)
    if account_id is not None:
        query = query.where(SyncState.account_id == account_id)
    return {(s.cloud, s.account_id, s.source): s for s in await session.scalars(query)}


def _connection(
    account: CloudAccountRow,
    states: dict[tuple[str, str, str], SyncState],
    now: datetime,
    settings: Settings,
) -> dict[str, Any]:
    def state(source: str) -> SyncState | None:
        return states.get((account.cloud, account.account_id, source))

    sources = {}
    for source in HEALTH_SOURCES:
        h = describe(
            source,
            state(source),
            now,
            account.access_status,
            account_id=account.account_id,
            principal_id=settings.azure_principal_id,
            cloud=account.cloud,
            aws_role_name=settings.aws_role_name,
        )
        message, fix = h.message, h.fix
        if account.cloud == "gcp":
            message, fix = _gcp_wording(account.account_id, message, fix, settings)
        elif account.cloud == "aws":
            raw = (state(source).last_error if state(source) else None) or ""
            message, fix = _aws_wording(account.account_id, message, fix, settings, raw)
        sources[source] = {
            "status": h.status,
            "last_success_at": utc_iso(h.last_success_at),
            "message": message,
            "fix": fix,
        }
    return {
        "cloud": account.cloud,
        "account_id": account.account_id,
        "name": account.name,
        "tracked": account.tracked,
        "access_status": account.access_status,
        "sources": sources,
        "backfill": {s: _backfill(state(s)) for s in BACKFILL_SOURCES},
    }


def _gcp_wording(
    project_id: str, message: str, fix: str | None, settings: Settings
) -> tuple[str, str | None]:
    """``health.describe`` speaks Azure (Reader, subscriptions, ``az``); say it the GCP way.

    Access fixes become the project's ``gcloud`` grant commands; an Azure credential hint
    becomes the WIF one.
    """
    message = message.replace(
        "Reader role missing on subscription", "Read-only roles missing on project"
    )
    message = message.replace("subscription", "project")
    if fix and "az role assignment" in fix:
        sa = settings.gcp_service_account or gcp_onboarding.SERVICE_ACCOUNT_PLACEHOLDER
        try:
            fix = gcp_onboarding.project_grant_commands(project_id, sa)
        except ValueError:  # Not a valid project id (old or hand-made row): no command.
            fix = None
    elif fix and "managed identity" in fix:
        message = "The app could not sign in to Google Cloud (Workload Identity Federation)"
        fix = "Check ACT_GCP_WIF_AUDIENCE and ACT_GCP_SERVICE_ACCOUNT (docs/gcp-setup.md)"
    return message, fix


# AWS errors that mean the role is missing or doesn't trust us (botocore codes / our
# AwsAccessDenied), which health.describe's Azure patterns don't recognise.
_AWS_DENIED = ("AccessDenied", "not authorized", "UnauthorizedOperation", "InvalidIdentityToken")


def _aws_wording(
    account_id: str, message: str, fix: str | None, settings: Settings, raw_error: str = ""
) -> tuple[str, str | None]:
    """``health.describe`` speaks Azure; say it the AWS way.

    Access fixes become the CloudFormation stack deploy command for that account; an Azure
    credential hint becomes the OIDC / AssumeRoleWithWebIdentity one.
    """
    stack_fix = (
        f"# Deploy the read-only role stack in AWS account {account_id} (AWS CloudShell)\n"
        + aws_onboarding.deploy_command(
            settings, export_account=aws_onboarding.is_export_account(settings, account_id)
        )
    )
    text = f"{message} {raw_error}".lower()
    if any(code.lower() in text for code in _AWS_DENIED):
        return f"Read-only role missing or not trusted in account {account_id}", stack_fix
    message = message.replace(
        "Reader role missing on subscription", "Read-only role missing in account"
    )
    message = message.replace("subscription", "account")
    if fix and ("az role assignment" in fix or "CloudFormation stack" in fix):
        # health.describe's generic advice; give the exact command for this account.
        fix = stack_fix
    elif fix and "managed identity" in fix:
        message = "The app could not sign in to AWS (Entra token for AssumeRoleWithWebIdentity)"
        fix = (
            "Check ACT_AZURE_PRINCIPAL_ID and the stack's EntraTenantId / Audience / "
            "IssuerVersion parameters (docs/aws-setup.md)"
        )
    return message, fix


def _backfill(row: SyncState | None) -> dict[str, Any] | None:
    if row is None or row.backfill_from is None:
        return None
    return {
        "progress": float(row.backfill_progress or 0),
        "from": row.backfill_from.isoformat(),
        "done_through": row.backfill_done_through.isoformat()
        if row.backfill_done_through
        else None,
    }
