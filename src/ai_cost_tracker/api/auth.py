"""Entra ID (v2.0) access-token validation and role-based access control.

The API is a resource server: the web app signs users in with MSAL and sends the access token
it got for our app registration. We validate it locally against the tenant's signing keys,
then map Entra groups and app roles onto the app's four roles.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, get_args

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWKClient
from pydantic import BaseModel

from ai_cost_tracker.config import Settings, get_settings

Role = Literal["owner", "admin", "analyst", "viewer"]
ROLES: frozenset[str] = frozenset(get_args(Role))

ALGORITHMS = ["RS256"]
LEEWAY_SECONDS = 60

OVERAGE_MESSAGE = (
    "Your account is in too many groups for Entra to include them in the token (groups "
    "overage). Configure the app registration to emit only the relevant group IDs "
    "(groupMembershipClaims=ApplicationGroup) or assign app roles instead."
)


class Principal(BaseModel):
    """The signed-in caller, as seen by route handlers."""

    oid: str
    name: str | None = None
    email: str | None = None
    groups: list[str] = []
    roles: set[Role] = set()


DEV_PRINCIPAL = Principal(oid="dev", name="Local developer", roles={"owner"})

# One JWKS client per tenant; it caches signing keys so we only hit Entra on key rotation.
_jwk_clients: dict[str, PyJWKClient] = {}


def check_auth_settings(settings: Settings) -> None:
    """Fail fast on unsafe or incomplete auth config. Call at startup; also run on each request."""
    if settings.auth_disabled:
        if settings.environment != "local":
            raise RuntimeError(
                f"ACT_AUTH_DISABLED=true is only allowed when ACT_ENVIRONMENT=local "
                f"(got {settings.environment!r})"
            )
        return
    if not settings.entra_tenant_id or not settings.entra_client_id:
        raise RuntimeError("ACT_ENTRA_TENANT_ID and ACT_ENTRA_CLIENT_ID must be set")


def _jwk_client(tenant_id: str) -> PyJWKClient:
    client = _jwk_clients.get(tenant_id)
    if client is None:
        client = PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )
        _jwk_clients[tenant_id] = client
    return client


async def _signing_key(token: str, tenant_id: str) -> Any:
    """Resolve the token's `kid` to a public key. PyJWKClient does blocking HTTP, so thread it."""
    jwk = await asyncio.to_thread(_jwk_client(tenant_id).get_signing_key_from_jwt, token)
    return jwk.key


def _unauthorized(description: str) -> HTTPException:
    quoted = description.replace('"', "'")
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=description,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{quoted}"'},
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


async def _decode(token: str, settings: Settings) -> dict[str, Any]:
    tenant = settings.entra_tenant_id
    try:
        # Reject other algorithms (notably HS256 / none) before any key lookup.
        if jwt.get_unverified_header(token).get("alg") not in ALGORITHMS:
            raise _unauthorized("Unsupported token algorithm")
        key = await _signing_key(token, tenant)
        claims = jwt.decode(
            token,
            key,
            algorithms=ALGORITHMS,
            audience=[settings.entra_client_id, f"api://{settings.entra_client_id}"],
            issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Token expired") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"Invalid token: {exc}") from exc
    if claims.get("tid") != tenant:
        raise _unauthorized("Token issued for another tenant")
    return claims


def _roles(claims: dict[str, Any], groups: set[str], settings: Settings) -> set[Role]:
    roles: set[str] = {r for r in claims.get("roles", []) if r in ROLES}
    for role, group_ids in settings.role_groups.items():
        if role in ROLES and groups.intersection(group_ids):
            roles.add(role)
    return roles  # type: ignore[return-value]


async def get_principal(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> Principal:
    """FastAPI dependency: the validated caller. 401 on a bad/missing token, 403 if not allowed."""
    check_auth_settings(settings)
    if settings.auth_disabled:
        return DEV_PRINCIPAL.model_copy(deep=True)

    claims = await _decode(_bearer_token(request), settings)
    groups = set(claims.get("groups", []))
    overage = "groups" in claims.get("_claim_names", {})
    roles = _roles(claims, groups, settings)

    if settings.allowed_group_ids:
        if overage:
            raise HTTPException(status.HTTP_403_FORBIDDEN, OVERAGE_MESSAGE)
        if not groups.intersection(settings.allowed_group_ids):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of an allowed group")
    elif overage and settings.role_groups and not roles:
        # Roles depend on group membership we cannot see; don't silently grant nothing.
        raise HTTPException(status.HTTP_403_FORBIDDEN, OVERAGE_MESSAGE)

    return Principal(
        oid=claims["oid"],
        name=claims.get("name"),
        email=claims.get("preferred_username") or claims.get("email") or claims.get("upn"),
        groups=sorted(groups),
        roles=roles,
    )


def require_role(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: the caller must hold one of `roles`. `owner` satisfies any role."""
    unknown = set(roles) - ROLES
    if not roles or unknown:
        raise ValueError(f"require_role needs one or more of {sorted(ROLES)}, got {roles}")
    wanted = set(roles)

    async def dependency(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        if "owner" in principal.roles or wanted & principal.roles:
            return principal
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"Requires one of the roles: {', '.join(sorted(wanted))}"
        )

    return dependency
