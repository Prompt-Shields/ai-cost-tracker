"""One-click "Connect Azure" endpoints (ARCHITECTURE.md §4.3, issues #14, #15, #25).

The browser sends the admin's delegated ARM token in ``X-Azure-Management-Token``. It is
used for this request only: never stored, never logged, only sent to management.azure.com.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.api.deps import SessionDep, SettingsDep, TenantId
from ai_cost_tracker.api.routes import connections
from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.azure import onboarding
from ai_cost_tracker.connectors.azure.onboarding import (
    DelegatedTokenRejected,
    SubscriptionCandidate,
)
from ai_cost_tracker.db.models import CloudAccountRow
from ai_cost_tracker.ingest import get_or_create_tenant

TOKEN_HEADER = "X-Azure-Management-Token"
CLOUD = "azure"

router = APIRouter(tags=["azure"], dependencies=[Depends(get_principal)])
Admin = Annotated[Principal, Depends(require_role("admin"))]


async def get_arm_http() -> AsyncIterator[httpx.AsyncClient | None]:
    """HTTP client for ARM calls; ``None`` lets onboarding make its own. Tests override it."""
    yield None


ArmHttp = Annotated[httpx.AsyncClient | None, Depends(get_arm_http)]


def arm_token(
    token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> str:
    if not token or not token.strip():
        raise HTTPException(400, f"Missing {TOKEN_HEADER} header (a delegated ARM token)")
    return token.strip()


ArmToken = Annotated[str, Depends(arm_token)]


class ConnectBody(BaseModel):
    subscription_ids: list[str]
    management_group_id: str | None = None


@router.post("/azure/discover")
async def discover(
    _: Admin, token: ArmToken, http: ArmHttp, session: SessionDep, tenant_id: TenantId
) -> dict[str, Any]:
    candidates = await _discover(token, http)
    known = await _accounts(session, tenant_id, [c.subscription_id for c in candidates])
    return {
        "subscriptions": [
            {
                "subscription_id": c.subscription_id,
                "name": c.name,
                "ai_accounts": c.ai_accounts,
                "ai_deployments": c.ai_deployments,
                "tracked": known[c.subscription_id].tracked if c.subscription_id in known else None,
                "access_status": known[c.subscription_id].access_status
                if c.subscription_id in known
                else None,
            }
            for c in candidates
        ]
    }


@router.post("/azure/connect")
async def connect(
    body: ConnectBody,
    _: Admin,
    token: ArmToken,
    http: ArmHttp,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    principal_id = _principal_id(settings)
    try:
        sub_scopes = {s: onboarding.subscription_scope(s) for s in body.subscription_ids}
        mg_scope = (
            onboarding.management_group_scope(body.management_group_id)
            if body.management_group_id
            else None
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    # Names and AI resource flags for the rows we create; one Resource Graph query.
    candidates = {c.subscription_id: c for c in await _discover(token, http)}
    tenant_id = await get_or_create_tenant(session, settings.default_tenant_slug)

    # Management-group option: one grant covers every selected subscription under it.
    grants: dict[str, tuple[str, str]] = {}  # subscription -> (status, scope for command)
    if mg_scope:
        result = await _grant(token, mg_scope, principal_id, http)
        for sub in sub_scopes:
            grants[sub] = (_status(result), mg_scope)
    else:
        for sub, scope in sub_scopes.items():
            grants[sub] = (_status(await _grant(token, scope, principal_id, http)), scope)

    now = datetime.now(UTC)
    results = []
    for sub, (status, scope) in grants.items():
        status = await _upsert(session, tenant_id, sub, candidates.get(sub), status, now)
        results.append(
            {
                "subscription_id": sub,
                "status": status,
                "command": onboarding.grant_command(scope, principal_id),
                "share_url": share_url(settings, sub),
            }
        )
    await session.commit()
    for r in results:
        if r["status"] == "connected":
            await connections.enqueue_backfill(r["subscription_id"], CLOUD)
    return {"results": results}


@router.get("/azure/pending/{subscription_id}")
async def pending(
    subscription_id: str, session: SessionDep, tenant_id: TenantId, settings: SettingsDep
) -> dict[str, Any]:
    """What the subscription owner sees when they open the share link."""
    try:
        scope = onboarding.subscription_scope(subscription_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    account = (await _accounts(session, tenant_id, [subscription_id])).get(subscription_id)
    if account is None:
        raise HTTPException(404, f"Subscription {subscription_id} is not being connected")
    return {
        "subscription_id": subscription_id,
        "name": account.name,
        "status": account.access_status,
        "command": onboarding.grant_command(scope, _principal_id(settings)),
        "share_url": share_url(settings, subscription_id),
    }


def share_url(settings: Settings, subscription_id: str) -> str:
    return f"{settings.web_base_url.rstrip('/')}/connect/pending/{subscription_id}"


def _principal_id(settings: Settings) -> str:
    if not settings.azure_principal_id:
        raise HTTPException(
            500,
            "ACT_AZURE_PRINCIPAL_ID is not set: configure the object id of the app's "
            "managed identity so it can be granted Reader",
        )
    return settings.azure_principal_id


def _status(result: onboarding.GrantResult) -> str:
    return "waiting_for_access" if result.status == "forbidden" else "connected"


async def _discover(token: str, http: httpx.AsyncClient | None) -> list[SubscriptionCandidate]:
    try:
        return await onboarding.discover_subscriptions(token, http)
    except DelegatedTokenRejected as exc:
        raise _bad_gateway(exc) from None
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502, f"Azure Resource Graph returned {exc.response.status_code}"
        ) from None


async def _grant(
    token: str, scope: str, principal_id: str, http: httpx.AsyncClient | None
) -> onboarding.GrantResult:
    try:
        return await onboarding.grant_reader(token, scope, principal_id, http)
    except DelegatedTokenRejected as exc:
        raise _bad_gateway(exc) from None
    except httpx.HTTPStatusError as exc:
        code, message = onboarding.arm_error(exc.response)
        detail = message or code or str(exc.response.status_code)
        raise HTTPException(
            502, f"Azure refused the role assignment on {scope}: {detail}"
        ) from None


def _bad_gateway(exc: DelegatedTokenRejected) -> HTTPException:
    return HTTPException(
        502,
        f"Azure rejected your sign-in token ({exc}). Sign in to Azure again and retry.",
    )


async def _accounts(
    session: AsyncSession, tenant_id: uuid.UUID, ids: list[str]
) -> dict[str, CloudAccountRow]:
    if not ids:
        return {}
    rows = await session.scalars(
        select(CloudAccountRow).where(
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.cloud == CLOUD,
            CloudAccountRow.account_id.in_(ids),
        )
    )
    return {r.account_id: r for r in rows}


async def _upsert(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    subscription_id: str,
    candidate: SubscriptionCandidate | None,
    status: str,
    now: datetime,
) -> str:
    """Track the subscription; returns the access status actually stored.

    A forbidden grant doesn't mean the app lacks access (someone may have granted Reader
    already), so an account that is already connected stays connected.
    """
    row = (await _accounts(session, tenant_id, [subscription_id])).get(subscription_id)
    existing = row is not None
    if row is None:
        row = CloudAccountRow(
            tenant_id=tenant_id, cloud=CLOUD, account_id=subscription_id, name=subscription_id
        )
        session.add(row)
    if candidate is not None:
        row.name = candidate.name
        row.has_ai_resources = candidate.ai_accounts > 0
    row.tracked = True
    if not (status == "waiting_for_access" and row.access_status == "connected" and existing):
        row.access_status = status
    row.discovered_at = row.discovered_at or now
    return row.access_status
