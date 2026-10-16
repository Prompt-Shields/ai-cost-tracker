"""Audit trail (ARCHITECTURE.md §7, issue #37).

Call ``record`` in every endpoint that changes something, in the same transaction as the
change, so a rolled-back change leaves no audit row and a committed one always has one.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.db.models import AuditLog


def record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal: Any,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Add an audit row to ``session`` (no commit). ``principal`` is an ``api.auth.Principal``.

    Never put secrets in ``details`` (webhook URLs, API keys, tokens).
    """
    row = AuditLog(
        tenant_id=tenant_id,
        at=datetime.now(UTC),
        actor_id=str(getattr(principal, "oid", "system")),
        actor_name=getattr(principal, "name", None),
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )
    session.add(row)
    return row
