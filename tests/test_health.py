"""Plain-language connection health from SyncState rows."""

from datetime import UTC, datetime, timedelta

import pytest

from ai_cost_tracker.db.models import SyncState
from ai_cost_tracker.health import describe

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SUB = "sub-1"
PRINCIPAL = "pid-1"


def _row(source="usage", success_ago=None, error=None, naive=False) -> SyncState:
    success = NOW - success_ago if success_ago is not None else None
    if success is not None and naive:
        success = success.replace(tzinfo=None)
    return SyncState(
        cloud="azure",
        account_id=SUB,
        source=source,
        last_attempt_at=NOW,
        last_success_at=success,
        last_error=error,
    )


def test_never_when_no_row():
    h = describe("cost", None, NOW, "connected", account_id=SUB)
    assert (h.status, h.last_success_at, h.fix) == ("never", None, None)


@pytest.mark.parametrize(
    ("source", "fresh", "stale"),
    [
        ("usage", timedelta(minutes=29), timedelta(minutes=31)),
        ("cost", timedelta(hours=11), timedelta(hours=13)),
        ("inventory", timedelta(hours=2), timedelta(hours=4)),
    ],
)
def test_staleness_thresholds(source, fresh, stale):
    assert describe(source, _row(source, fresh), NOW, "connected").status == "ok"
    h = describe(source, _row(source, stale), NOW, "connected")
    assert h.status == "stale"
    assert h.fix


def test_naive_sqlite_datetimes_are_utc():
    h = describe("usage", _row(success_ago=timedelta(minutes=5), naive=True), NOW, "connected")
    assert h.status == "ok"
    assert h.last_success_at == NOW - timedelta(minutes=5)


def test_forbidden_error_gives_grant_command():
    err = "Client error '403 Forbidden' for url 'https://management.azure.com/...'"
    h = describe(
        "cost", _row("cost", timedelta(days=2), err), NOW, "connected", principal_id=PRINCIPAL
    )
    assert h.status == "error"
    assert h.message == f"Reader role missing on subscription {SUB}"
    assert h.fix.startswith("az role assignment create")
    assert f"--assignee-object-id {PRINCIPAL}" in h.fix
    assert f"--scope /subscriptions/{SUB}" in h.fix
    assert h.last_success_at == NOW - timedelta(days=2)

    h = describe("cost", _row("cost", error="AuthorizationFailed: no"), NOW, "connected")
    assert "Reader role missing" in h.message


def test_credential_error():
    h = describe("usage", _row(error="CredentialUnavailableError: ..."), NOW, "connected")
    assert h.status == "error"
    assert "managed identity" in h.message
    assert h.fix


def test_throttled_error():
    h = describe("usage", _row(error="Client error '429 Too Many Requests'"), NOW, "connected")
    assert h.message == "Azure is throttling requests; will retry"


def test_other_error_first_line():
    h = describe("usage", _row(error="boom happened\nTraceback..."), NOW, "connected")
    assert (h.status, h.message, h.fix) == ("error", "boom happened", None)


def test_waiting_for_access():
    h = describe("cost", None, NOW, "waiting_for_access", account_id=SUB, principal_id=PRINCIPAL)
    assert h.status == "never"
    assert h.message == "Waiting for someone to grant access"
    assert "--role Reader" in h.fix and SUB in h.fix


def test_waiting_without_principal_uses_placeholder():
    h = describe("cost", None, NOW, "waiting_for_access", account_id=SUB)
    assert "<ACT_AZURE_PRINCIPAL_ID>" in h.fix
