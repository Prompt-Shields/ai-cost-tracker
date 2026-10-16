"""API keys (issue #35): create once, authenticate by hash, role limits, immediate revoke."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker import api_keys
from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import ApiKey, AuditLog, Tenant
from ai_cost_tracker.db.session import get_session
from tests.test_auth import CLIENT, TENANT, fake_jwks, token  # noqa: F401 (autouse fixture)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        entra_tenant_id=TENANT,
        entra_client_id=CLIENT,
        environment="test",
        default_tenant_slug="internal",
    )


@pytest.fixture
async def client(engine, tenant_id):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def jwt_for(*roles: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(roles=list(roles))}"}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


OWNER = jwt_for("owner")
ADMIN = jwt_for("admin")


async def _create(client, headers=OWNER, **body) -> httpx.Response:
    return await client.post("/api/v1/api-keys", json={"name": "ci", **body}, headers=headers)


async def _row(session: AsyncSession, key_id: str) -> ApiKey:
    session.expire_all()
    return (await session.scalars(select(ApiKey).where(ApiKey.id == uuid.UUID(key_id)))).one()


def test_key_format_and_hash():
    new = api_keys.generate()
    assert new.key.startswith("act_")
    assert len(new.key) == 4 + 43  # 32 bytes, url-safe base64 without padding
    assert new.prefix == new.key[4:12]
    assert new.key_hash == api_keys.hash_key(new.key)
    assert len(new.key_hash) == 64
    assert api_keys.generate().key != new.key


async def test_lifecycle(client, session: AsyncSession, tenant_id):
    resp = await _create(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    key = body["key"]
    assert key.startswith("act_")
    assert body["prefix"] == key[4:12]
    assert body["roles"] == ["viewer"]
    assert body["last_used_at"] is None and body["revoked_at"] is None
    assert "key_hash" not in body

    row = await _row(session, body["id"])
    assert row.key_hash == api_keys.hash_key(key)
    assert key not in (row.key_hash, row.prefix)

    # Listed without the key or its hash.
    listed = (await client.get("/api/v1/api-keys", headers=OWNER)).json()
    assert [k["id"] for k in listed] == [body["id"]]
    assert "key" not in listed[0] and "key_hash" not in listed[0]

    # The key authenticates and acts with its roles.
    me = (await client.get("/api/v1/me", headers=bearer(key))).json()
    assert me["oid"] == f"apikey:{body['id']}"
    assert me["name"] == "ci"
    assert me["roles"] == ["viewer"]
    assert (await client.get("/api/v1/summary", headers=bearer(key))).status_code == 200
    first_use = (await _row(session, body["id"])).last_used_at
    assert first_use is not None

    # Revoke: the very next request fails.
    resp = await client.post(f"/api/v1/api-keys/{body['id']}/revoke", headers=OWNER)
    assert resp.status_code == 200
    revoked_at = resp.json()["revoked_at"]
    assert revoked_at is not None
    resp = await client.get("/api/v1/summary", headers=bearer(key))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")

    # Revoking again is a no-op that keeps the first timestamp.
    again = await client.post(f"/api/v1/api-keys/{body['id']}/revoke", headers=OWNER)
    assert again.json()["revoked_at"] == revoked_at

    # Audit: create and revoke, with the prefix but never the key or hash.
    audits = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.tenant_id == tenant_id, AuditLog.action.like("api_key.%"))
            .order_by(AuditLog.id)
        )
    ).all()
    assert [a.action for a in audits] == ["api_key.create", "api_key.revoke"]
    for a in audits:
        assert a.target_id == body["id"]
        assert a.details["prefix"] == body["prefix"]
        assert key not in str(a.details) and row.key_hash not in str(a.details)


async def test_last_used_at_written_at_most_once_a_minute(session: AsyncSession, tenant_id):
    new = api_keys.generate()
    session.add(
        ApiKey(
            tenant_id=tenant_id,
            name="k",
            prefix=new.prefix,
            key_hash=new.key_hash,
            roles=["viewer"],
            created_by="t",
        )
    )
    await session.commit()
    t0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    row = await api_keys.authenticate(session, "internal", new.key, now=t0)
    assert row is not None and row.last_used_at == t0
    row = await api_keys.authenticate(session, "internal", new.key, now=t0 + timedelta(seconds=59))
    session.expire_all()
    stored = await session.scalar(select(ApiKey.last_used_at))
    assert stored.replace(tzinfo=UTC) == t0
    later = t0 + timedelta(seconds=61)
    await api_keys.authenticate(session, "internal", new.key, now=later)
    session.expire_all()
    stored = await session.scalar(select(ApiKey.last_used_at))
    assert stored.replace(tzinfo=UTC) == later


async def test_key_scoped_to_tenant(session: AsyncSession, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    new = api_keys.generate()
    session.add(
        ApiKey(tenant_id=other.id, name="k", prefix=new.prefix, key_hash=new.key_hash,
               roles=["viewer"], created_by="t")
    )  # fmt: skip
    await session.commit()
    assert await api_keys.authenticate(session, "internal", new.key) is None
    assert await api_keys.authenticate(session, "other", new.key) is not None


@pytest.mark.parametrize("key", ["act_" + "x" * 43, "act_"])
async def test_unknown_key_rejected(client, key):
    assert (await client.get("/api/v1/summary", headers=bearer(key))).status_code == 401


async def test_role_limits(client):
    # Only an owner may create admin keys.
    assert (await _create(client, ADMIN, roles=["admin"])).status_code == 403
    assert (await _create(client, ADMIN, roles=["analyst", "viewer"])).status_code == 201
    resp = await _create(client, OWNER, roles=["admin"])
    assert resp.status_code == 201
    admin_key = resp.json()["key"]
    # Never owner; never empty; never unknown.
    for roles in (["owner"], [], ["superuser"]):
        assert (await _create(client, OWNER, roles=roles)).status_code == 422
    # Viewers and analysts can't manage keys.
    assert (await _create(client, jwt_for("viewer"))).status_code == 403
    assert (await client.get("/api/v1/api-keys", headers=jwt_for("analyst"))).status_code == 403
    # A key acts with exactly its roles: the admin key passes require_role("admin")...
    assert (await client.get("/api/v1/api-keys", headers=bearer(admin_key))).status_code == 200
    # ...but no key may mint or revoke keys.
    assert (await _create(client, bearer(admin_key))).status_code == 403
    viewer_key = (await _create(client)).json()
    resp = await client.post(
        f"/api/v1/api-keys/{viewer_key['id']}/revoke", headers=bearer(admin_key)
    )
    assert resp.status_code == 403
    assert (
        await client.get("/api/v1/api-keys", headers=bearer(viewer_key["key"]))
    ).status_code == 403


async def test_revoke_unknown_key(client):
    resp = await client.post(f"/api/v1/api-keys/{uuid.uuid4()}/revoke", headers=OWNER)
    assert resp.status_code == 404


async def test_jwt_still_works_alongside_keys(client):
    await _create(client)
    me = (await client.get("/api/v1/me", headers=OWNER)).json()
    assert me["oid"] == "user-1"
    assert me["roles"] == ["owner"]
