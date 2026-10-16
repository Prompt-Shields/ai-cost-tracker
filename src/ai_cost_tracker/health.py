"""Turn SyncState rows into the plain-language "Connection health" panel (§4.3, issue #24).

Admins shouldn't have to read stack traces: each source gets a status, a sentence saying what
is going on and, when something is wrong, a one-line fix.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from ai_cost_tracker.connectors.azure.onboarding import grant_command
from ai_cost_tracker.db.models import SyncState

Status = Literal["ok", "stale", "error", "never"]

# How long after the last success a source counts as stale. Usage is pulled every few
# minutes, inventory hourly, cost a few times a day (ARCHITECTURE.md §5).
STALE_AFTER: dict[str, timedelta] = {
    "usage": timedelta(minutes=30),
    "cost": timedelta(hours=12),
    "inventory": timedelta(hours=3),
}
LABELS = {"usage": "token usage", "cost": "cost", "inventory": "inventory"}
PRINCIPAL_PLACEHOLDER = "<ACT_AZURE_PRINCIPAL_ID>"

_FORBIDDEN = re.compile(r"\b403\b|AuthorizationFailed", re.IGNORECASE)
_UNAUTHENTICATED = re.compile(
    r"\b401\b|credential|ManagedIdentity|ClientAuthenticationError|InvalidAuthenticationToken",
    re.IGNORECASE,
)
_THROTTLED = re.compile(r"\b429\b|TooManyRequests|throttl", re.IGNORECASE)


@dataclass
class SourceHealth:
    status: Status
    last_success_at: datetime | None
    message: str
    fix: str | None


def describe(
    source: str,
    row: SyncState | None,
    now: datetime,
    access_status: str,
    *,
    account_id: str | None = None,
    principal_id: str = "",
) -> SourceHealth:
    """Health of one source of one account. ``account_id`` defaults to ``row.account_id``."""
    account_id = account_id or (row.account_id if row else "") or "<subscription-id>"
    command = grant_command(f"/subscriptions/{account_id}", principal_id or PRINCIPAL_PLACEHOLDER)
    last_success = _utc(row.last_success_at) if row else None
    now = _utc(now) or datetime.now(UTC)

    if access_status == "waiting_for_access":
        return SourceHealth("never", last_success, "Waiting for someone to grant access", command)
    if access_status == "no_access":
        return SourceHealth(
            "error", last_success, f"Reader role missing on subscription {account_id}", command
        )
    if row is not None and row.last_error:
        message, fix = _explain(row.last_error, account_id, command)
        return SourceHealth("error", last_success, message, fix)
    label = LABELS.get(source, source)
    if last_success is None:
        return SourceHealth("never", None, f"No {label} pulled yet", None)
    age = now - last_success
    if age > STALE_AFTER.get(source, timedelta(hours=12)):
        return SourceHealth(
            "stale",
            last_success,
            f"Last {label} pull was {_ago(age)} ago",
            "Check that the worker is running (docker compose ps / Container Apps logs)",
        )
    return SourceHealth("ok", last_success, f"Last {label} pull {_ago(age)} ago", None)


def _explain(error: str, account_id: str, command: str) -> tuple[str, str | None]:
    """Map a stored error to (plain-language message, one-line fix)."""
    if _FORBIDDEN.search(error):
        return f"Reader role missing on subscription {account_id}", command
    if _UNAUTHENTICATED.search(error):
        return (
            "The app's managed identity is not configured or Azure rejected its credentials",
            "Enable a managed identity on the app and grant it Reader (locally: run az login)",
        )
    if _THROTTLED.search(error):
        return "Azure is throttling requests; will retry", None
    first = error.strip().splitlines()[0] if error.strip() else "Unknown error"
    return first[:300], None


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; we only ever store UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ago(age: timedelta) -> str:
    minutes = max(int(age.total_seconds() // 60), 0)
    if minutes < 1:
        return "less than a minute"
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"
