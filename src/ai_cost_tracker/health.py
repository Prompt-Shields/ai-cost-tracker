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
# AWS access is the read-only role our CloudFormation stack creates in each account
# (docs/aws-setup.md); the stack name matches connectors/aws/onboarding.deploy_command.
AWS_STACK_NAME = "ai-cost-tracker-readonly"
AWS_DEFAULT_ROLE = "AiCostTrackerReadOnly"

_FORBIDDEN = re.compile(r"\b403\b|AuthorizationFailed", re.IGNORECASE)
_UNAUTHENTICATED = re.compile(
    r"\b401\b|credential|ManagedIdentity|ClientAuthenticationError|InvalidAuthenticationToken",
    re.IGNORECASE,
)
_THROTTLED = re.compile(r"\b429\b|TooManyRequests|throttl", re.IGNORECASE)
# AwsAccessDenied / AWS error codes (connectors/aws/auth.py maps ClientErrors to these).
_AWS_DENIED = re.compile(
    r"AwsAccessDenied|AccessDenied|NotAuthorized|UnauthorizedOperation|AuthorizationError"
    r"|InvalidIdentityToken|not authorized|role (?:can't|cannot) be assumed",
    re.IGNORECASE,
)
_AWS_AUTH = re.compile(r"AwsAuthError|ExpiredToken|IDPCommunicationError", re.IGNORECASE)


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
    cloud: str = "azure",
    aws_role_name: str = AWS_DEFAULT_ROLE,
) -> SourceHealth:
    """Health of one source of one account. ``account_id`` defaults to ``row.account_id``.

    Wording is Azure's (Reader role, ``az``) unless ``cloud="aws"``, where access is the
    read-only IAM role from our CloudFormation stack and the fix is to (re-)deploy it.
    GCP rewrites the Azure wording in ``api/routes/connections.py``.
    """
    if cloud == "aws":
        return _describe_aws(source, row, now, access_status, account_id, aws_role_name)
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
    return _freshness(source, last_success, now)


def _freshness(source: str, last_success: datetime | None, now: datetime) -> SourceHealth:
    """ok / stale / never from the last successful pull, the same on every cloud."""
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


def aws_fix(account_id: str, role_name: str = AWS_DEFAULT_ROLE) -> str:
    """One-line fix for AWS access problems: (re-)deploy the stack that creates the role."""
    return (
        f"Deploy (or re-deploy) the {AWS_STACK_NAME} CloudFormation stack in AWS account "
        f"{account_id} and check that role {role_name} trusts this app (docs/aws-setup.md)"
    )


def _describe_aws(
    source: str,
    row: SyncState | None,
    now: datetime,
    access_status: str,
    account_id: str | None,
    role_name: str,
) -> SourceHealth:
    account_id = account_id or (row.account_id if row else "") or "<account-id>"
    fix = aws_fix(account_id, role_name)
    last_success = _utc(row.last_success_at) if row else None
    now = _utc(now) or datetime.now(UTC)
    if access_status == "waiting_for_access":
        return SourceHealth(
            "never", last_success, "Waiting for the CloudFormation stack to be deployed", fix
        )
    if access_status == "no_access":
        return SourceHealth(
            "error", last_success, f"Can't assume role {role_name} in account {account_id}", fix
        )
    if row is not None and row.last_error:
        message, hint = _explain_aws(row.last_error, account_id, role_name, fix)
        return SourceHealth("error", last_success, message, hint)
    return _freshness(source, last_success, now)


def _explain_aws(error: str, account_id: str, role_name: str, fix: str) -> tuple[str, str | None]:
    """Map a stored AWS error to (plain-language message, one-line fix)."""
    if _AWS_DENIED.search(error) or _FORBIDDEN.search(error):
        return (
            f"Can't assume role {role_name} in account {account_id}, or it lacks a permission",
            fix,
        )
    if _AWS_AUTH.search(error) or _UNAUTHENTICATED.search(error):
        return (
            "The app could not exchange its Azure managed identity token for AWS credentials",
            "Check the IAM OIDC identity provider's issuer and audience in the stack, and "
            "ACT_AWS_AZURE_TOKEN_SCOPE (docs/aws-setup.md)",
        )
    if _THROTTLED.search(error):
        return "AWS is throttling requests; will retry", None
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
