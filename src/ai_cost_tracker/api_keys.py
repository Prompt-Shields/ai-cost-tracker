"""API keys for programmatic access (issue #35, ARCHITECTURE.md §7).

A key is ``act_`` + 32 random bytes (url-safe base64). Only its SHA-256 is stored, plus the
first characters after ``act_`` so people can tell keys apart in the UI. Validity is checked
against the database on every request (no cache), so a revoked key stops working at once.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import ApiKey, Tenant

KEY_PREFIX = "act_"
DISPLAY_PREFIX_LEN = 8
KEY_ROLES: frozenset[str] = frozenset({"admin", "analyst", "viewer"})  # never "owner"
DEFAULT_ROLES = ["viewer"]
# Don't write last_used_at on every request: once a minute is precise enough for "last used".
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True)
class NewKey:
    key: str  # the full secret; shown to the caller once, never stored
    prefix: str
    key_hash: str


def hash_key(key: str) -> str:
    """SHA-256 hex of the full key.

    A fast, unsalted hash is fine here (unlike passwords): keys carry 256 bits of randomness,
    so there is nothing to brute-force or look up in a rainbow table. It also lets us find the
    key by an indexed equality lookup on the hash.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key.removeprefix(KEY_PREFIX)[:DISPLAY_PREFIX_LEN]


def generate() -> NewKey:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(key=key, prefix=display_prefix(key), key_hash=hash_key(key))


def looks_like_key(token: str) -> bool:
    return token.startswith(KEY_PREFIX)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on the way back; we only ever store UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def authenticate(
    session: AsyncSession, tenant_slug: str, key: str, *, now: datetime | None = None
) -> ApiKey | None:
    """The unrevoked key matching ``key`` in the tenant, or None. Touches ``last_used_at``."""
    if not looks_like_key(key):
        return None
    digest = hash_key(key)
    row = await session.scalar(
        select(ApiKey)
        .join(Tenant, Tenant.id == ApiKey.tenant_id)
        .where(
            Tenant.slug == tenant_slug,
            ApiKey.key_hash == digest,
            ApiKey.revoked_at.is_(None),
        )
    )
    # The lookup already matched on the hash; compare again in constant time anyway, so the
    # check doesn't depend on how the database compares strings.
    if row is None or not hmac.compare_digest(row.key_hash, digest):
        return None
    now = now or datetime.now(UTC)
    last = _aware(row.last_used_at)
    if last is None or now - last >= LAST_USED_RESOLUTION:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == row.id, ApiKey.tenant_id == row.tenant_id)
            .values(last_used_at=now)
        )
        await session.commit()
        row.last_used_at = now
    return row


async def list_keys(session: AsyncSession, tenant_id: uuid.UUID) -> list[ApiKey]:
    rows = await session.scalars(
        select(ApiKey)
        .where(ApiKey.tenant_id == tenant_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.name)
    )
    return list(rows)


async def get_key(session: AsyncSession, tenant_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
    return await session.scalar(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id, ApiKey.id == key_id)
    )
