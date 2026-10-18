"""Entra token validation and RBAC, against locally signed tokens (no network)."""

import time
from typing import Annotated, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from ai_cost_tracker import api_keys
from ai_cost_tracker.api import auth
from ai_cost_tracker.api.auth import Principal, get_principal, require_role
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.session import get_session

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
ALLOWED = "group-allowed"
ADMINS = "group-admins"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch):
    async def signing_key(token: str, tenant_id: str):
        assert tenant_id == TENANT
        return PRIVATE_KEY.public_key()

    monkeypatch.setattr(auth, "_signing_key", signing_key)


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "entra_tenant_id": TENANT,
        "entra_client_id": CLIENT,
        "environment": "test",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def make_client(settings: Settings) -> TestClient:
    app = FastAPI()

    @app.get("/me")
    async def me(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
        return principal

    admin_only = require_role("admin")

    @app.get("/admin")
    async def admin(principal: Annotated[Principal, Depends(admin_only)]) -> dict:
        return {"oid": principal.oid}

    app.dependency_overrides[get_settings] = lambda: settings
    # JWTs never touch the database; API-key tests stub ``api_keys.authenticate``.
    app.dependency_overrides[get_session] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def token(alg: str = "RS256", key: Any = PRIVATE_KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT,
        "tid": TENANT,
        "oid": "user-1",
        "name": "Ada Lovelace",
        "preferred_username": "ada@example.com",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "groups": [ALLOWED],
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "test"})


def get(client: TestClient, path: str, tok: str | None):
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    return client.get(path, headers=headers)


def test_valid_token():
    resp = get(make_client(make_settings()), "/me", token())
    assert resp.status_code == 200
    body = resp.json()
    assert body["oid"] == "user-1"
    assert body["email"] == "ada@example.com"
    assert body["groups"] == [ALLOWED]
    assert body["roles"] == []


def test_api_uri_audience_accepted():
    assert get(make_client(make_settings()), "/me", token(aud=f"api://{CLIENT}")).status_code == 200


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 120},  # expired beyond the 60s leeway
        {"aud": "someone-else"},
        {"iss": "https://login.microsoftonline.com/other/v2.0"},
        {"tid": "33333333-3333-3333-3333-333333333333"},
        {"oid": None},
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "wrong-tid", "no-oid"],
)
def test_invalid_token_rejected(claims):
    resp = get(make_client(make_settings()), "/me", token(**claims))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith('Bearer error="invalid_token"')


def test_expiry_within_leeway_accepted():
    tok = token(exp=int(time.time()) - 30)
    assert get(make_client(make_settings()), "/me", tok).status_code == 200


def test_hs256_rejected():
    tok = token(alg="HS256", key="a-shared-secret-that-is-long-enough-for-hs256")
    resp = get(make_client(make_settings()), "/me", tok)
    assert resp.status_code == 401


def test_wrong_signing_key_rejected():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert get(make_client(make_settings()), "/me", token(key=other)).status_code == 401


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer "])
def test_missing_bearer(header):
    client = make_client(make_settings())
    resp = client.get("/me", headers={"Authorization": header} if header else {})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


def test_group_allow_list():
    client = make_client(make_settings(allowed_group_ids=[ALLOWED]))
    assert get(client, "/me", token()).status_code == 200
    resp = get(client, "/me", token(groups=["some-other-group"]))
    assert resp.status_code == 403


def test_groups_overage_rejected_with_guidance():
    client = make_client(make_settings(allowed_group_ids=[ALLOWED]))
    tok = token(groups=None, _claim_names={"groups": "src1"}, _claim_sources={"src1": {}})
    resp = get(client, "/me", tok)
    assert resp.status_code == 403
    assert "app roles" in resp.json()["detail"]


def test_role_mapping_from_groups_and_app_roles():
    client = make_client(make_settings(role_groups={"admin": [ADMINS], "viewer": [ALLOWED]}))
    body = get(client, "/me", token(groups=[ADMINS, ALLOWED], roles=["analyst", "Other"])).json()
    assert set(body["roles"]) == {"admin", "viewer", "analyst"}


def test_require_role():
    client = make_client(make_settings(role_groups={"admin": [ADMINS]}))
    assert get(client, "/admin", token(groups=[ADMINS])).status_code == 200
    assert get(client, "/admin", token(roles=["owner"])).status_code == 200  # owner implies all
    resp = get(client, "/admin", token(roles=["viewer"]))
    assert resp.status_code == 403


def test_require_role_rejects_unknown_role():
    with pytest.raises(ValueError):
        require_role("superuser")  # type: ignore[arg-type]


def test_dev_bypass_local():
    client = make_client(make_settings(auth_disabled=True, environment="local"))
    resp = client.get("/admin")
    assert resp.status_code == 200
    body = get(client, "/me", None).json()
    assert body["oid"] == "dev"
    assert body["name"] == "Local developer"
    assert body["roles"] == ["owner"]


@pytest.mark.parametrize("environment", ["test", "production"])
def test_dev_bypass_refused_outside_local(environment):
    settings = make_settings(auth_disabled=True, environment=environment)
    with pytest.raises(RuntimeError, match="only allowed"):
        auth.check_auth_settings(settings)
    assert get(make_client(settings), "/me", token()).status_code == 500


def test_jwk_client_cached_per_tenant():
    auth._jwk_clients.clear()
    first = auth._jwk_client(TENANT)
    assert auth._jwk_client(TENANT) is first
    assert auth._jwk_client("other") is not first
    assert first.uri == f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    auth._jwk_clients.clear()


class _KeyRow:
    id = "k-1"
    name = "ci"
    roles = ["admin", "owner", "bogus"]  # "owner" is never honoured for a key


@pytest.fixture
def key_lookup(monkeypatch):
    calls: list[str] = []

    async def authenticate(session, tenant_slug, key, *, now=None):
        calls.append(key)
        return _KeyRow() if key == "act_good" else None

    monkeypatch.setattr(api_keys, "authenticate", authenticate)
    return calls


def test_api_key_principal(key_lookup):
    client = make_client(make_settings())
    body = get(client, "/me", "act_good").json()
    assert body == {
        "oid": "apikey:k-1",
        "name": "ci",
        "email": None,
        "groups": [],
        "roles": ["admin"],
    }
    assert get(client, "/admin", "act_good").status_code == 200
    assert key_lookup == ["act_good", "act_good"]


def test_unknown_or_revoked_api_key_401(key_lookup):
    resp = get(make_client(make_settings()), "/me", "act_revoked")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith('Bearer error="invalid_token"')


def test_jwt_path_never_looks_up_keys(key_lookup):
    assert get(make_client(make_settings()), "/me", token()).status_code == 200
    assert key_lookup == []


def test_dev_bypass_ignores_keys(key_lookup):
    client = make_client(make_settings(auth_disabled=True, environment="local"))
    assert get(client, "/me", "act_revoked").json()["oid"] == "dev"
    assert key_lookup == []
