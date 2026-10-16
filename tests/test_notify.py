"""Notifications: payload shapes, signatures, filtering, retries and the daily digest."""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import AlertEvent, NotificationChannel, NotificationDelivery
from ai_cost_tracker.notify import (
    NotifyError,
    run_daily_digest,
    run_notifications,
    send_test,
    senders,
)
from ai_cost_tracker.notify.channels import mask_target, matches, validate_target
from ai_cost_tracker.notify.jobs import MAX_ATTEMPTS

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
SLACK = "https://hooks.slack.com/services/T000/B000/secrettokenabcd"
TEAMS = "https://prod-00.westeurope.logic.azure.com/workflows/xyz/triggers/manual?sig=s3cr3t9876"
HOOK = "https://example.com/hooks/act?token=zzzz1234"


def _settings(**kw) -> Settings:
    base = {
        "environment": "test",
        "default_tenant_slug": "internal",
        "web_base_url": "https://act.example.com",
    }
    return Settings(**(base | kw))


class Recorder:
    """MockTransport handler that records requests and can fail on demand."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="ok" if self.status < 300 else "nope")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[str] = []

    def __enter__(self):
        if FakeSMTP.fail:
            raise ConnectionRefusedError("connection refused")
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append((self, msg, to_addrs))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr(senders.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def _channel(tenant_id, kind, target, **kw) -> NotificationChannel:
    return NotificationChannel(
        tenant_id=tenant_id, name=kw.pop("name", kind), kind=kind, target=target, **kw
    )


def _alert(tenant_id, kind="budget", severity="warning", subject="s", **kw) -> AlertEvent:
    return AlertEvent(
        tenant_id=tenant_id,
        kind=kind,
        subject=subject,
        severity=severity,
        message=kw.pop("message", f"{kind} {subject} needs attention"),
        details=kw.pop("details", {"budget": "Team A", "percent_used": "85"}),
        created_at=kw.pop("created_at", NOW - timedelta(hours=1)),
        **kw,
    )


async def _deliveries(session) -> list[NotificationDelivery]:
    session.expire_all()
    return list(
        await session.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))
    )


# --- payload shapes ------------------------------------------------------------------


async def test_alert_delivered_to_each_channel_kind(session, tenant_id, factory, smtp):
    session.add_all(
        [
            _channel(tenant_id, "slack", SLACK),
            _channel(tenant_id, "teams", TEAMS),
            _channel(tenant_id, "webhook", HOOK, signing_secret="s" * 32),
            _channel(tenant_id, "email", "finops@example.com"),
            _alert(tenant_id, severity="critical"),
        ]
    )
    await session.commit()
    rec = Recorder()
    settings = _settings(smtp_host="smtp.example.com", smtp_username="u", smtp_password="p")
    async with rec.client() as client:
        sent = await run_notifications(factory, settings, now=NOW, client=client)
    assert sent == 4

    by_url = {str(r.url): r for r in rec.requests}
    slack = json.loads(by_url[SLACK].content)
    assert slack["text"].startswith("[CRITICAL] Budget")
    assert slack["blocks"][0]["type"] == "section"
    assert "https://act.example.com/alerts" in json.dumps(slack["blocks"])

    teams = json.loads(by_url[TEAMS].content)
    assert teams["type"] == "message"
    att = teams["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard"
    assert att["content"]["body"][0]["text"].startswith("[CRITICAL]")
    assert att["content"]["actions"][0]["url"] == "https://act.example.com/alerts"

    hook = json.loads(by_url[HOOK].content)
    assert hook["event"]["kind"] == "budget"
    assert hook["event"]["severity"] == "critical"
    assert hook["event"]["details"]["budget"] == "Team A"
    assert hook["sent_at"] == NOW.isoformat()

    ((conn, email, to),) = smtp.sent
    assert to == ["finops@example.com"]
    assert email["Subject"].startswith("[CRITICAL]")
    assert "https://act.example.com/alerts" in email.get_content()
    assert conn.calls == ["starttls", "login:u"]

    rows = await _deliveries(session)
    assert sorted(r.status for r in rows) == ["sent"] * 4
    alert = await session.scalar(select(AlertEvent))
    assert alert.notified_at is not None


async def test_webhook_signature_verifies(session, tenant_id):
    secret = "x" * 40
    channel = _channel(tenant_id, "webhook", HOOK, signing_secret=secret)
    rec = Recorder()
    async with rec.client() as client:
        await send_test(channel, _settings(), client=client, now=NOW)
    req = rec.requests[0]
    signed = req.headers["X-ACT-Timestamp"].encode() + b"." + req.content
    expected = "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    assert req.headers["X-ACT-Signature"] == expected
    assert req.headers["X-ACT-Timestamp"] == str(int(NOW.timestamp()))
    assert json.loads(req.content)["event"]["kind"] == "test"


async def test_email_disabled_without_smtp_host(tenant_id, smtp):
    channel = _channel(tenant_id, "email", "a@example.com")
    with pytest.raises(NotifyError, match="ACT_SMTP_HOST"):
        await send_test(channel, _settings())
    assert smtp.sent == []


async def test_http_target_refused_outside_local(tenant_id):
    channel = _channel(tenant_id, "slack", "http://hooks.slack.com/services/x")
    rec = Recorder()
    async with rec.client() as client:
        with pytest.raises(NotifyError, match="https"):
            await send_test(channel, _settings(), client=client)
        await send_test(channel, _settings(environment="local"), client=client)
    assert len(rec.requests) == 1


async def test_errors_never_contain_the_full_url(tenant_id):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    channel = _channel(tenant_id, "slack", SLACK)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotifyError) as err:
            await send_test(channel, _settings(), client=client)
    assert "secrettoken" not in str(err.value)
    assert "hooks.slack.com" in str(err.value)


# --- matching, masking, validation -----------------------------------------------------


def test_masking():
    assert mask_target("slack", SLACK) == "https://hooks.slack.com/…/abcd"
    assert mask_target("email", "finops@example.com") == "f…@example.com"
    assert "s3cr3t" not in mask_target("teams", TEAMS)


def test_validate_target():
    assert validate_target("email", " a@b.co ", environment="test") == "a@b.co"
    for kind, target in [
        ("email", "not-an-email"),
        ("slack", "ftp://x"),
        ("webhook", "http://example.com"),
        ("webhook", "https://user:pw@example.com"),
    ]:
        with pytest.raises(ValueError):
            validate_target(kind, target, environment="test")


def test_matches():
    ch = NotificationChannel(alert_kinds=["budget"], min_severity="warning", kind="slack")
    assert matches(ch, "budget", "critical")
    assert not matches(ch, "budget", "info")
    assert not matches(ch, "variance", "critical")
    ch.alert_kinds = []
    assert matches(ch, "variance", "warning")


async def test_filters_by_kind_and_severity(session, tenant_id, factory):
    session.add_all(
        [
            _channel(tenant_id, "slack", SLACK, alert_kinds=["budget"], min_severity="warning"),
            _channel(tenant_id, "webhook", HOOK, min_severity="critical", signing_secret="k" * 20),
            _channel(tenant_id, "teams", TEAMS, digest=True),  # digest-only: skipped here
            _channel(tenant_id, "teams", TEAMS + "x", active=False),
            _alert(tenant_id, "budget", "warning", "b1"),
            _alert(tenant_id, "variance", "critical", "v1"),
            _alert(tenant_id, "access_lost", "info", "a1"),
            _alert(tenant_id, "budget", "critical", "old", resolved_at=NOW),
        ]
    )
    await session.commit()
    rec = Recorder()
    async with rec.client() as client:
        assert await run_notifications(factory, _settings(), now=NOW, client=client) == 2
        # Second run: nothing left to send.
        assert await run_notifications(factory, _settings(), now=NOW, client=client) == 0
    assert sorted(str(r.url) for r in rec.requests) == sorted([SLACK, HOOK])
    alerts = {a.subject: a for a in await session.scalars(select(AlertEvent))}
    # Every pending alert is marked handled, even the one no channel wanted.
    assert alerts["a1"].notified_at is not None
    assert alerts["old"].notified_at is None


async def test_retry_then_give_up(session, tenant_id, factory):
    session.add_all(
        [
            _channel(tenant_id, "slack", SLACK, name="good"),
            _channel(tenant_id, "teams", TEAMS, name="bad"),
            _alert(tenant_id),
        ]
    )
    await session.commit()

    def handler(request):
        return httpx.Response(500 if "logic.azure.com" in str(request.url) else 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for i in range(MAX_ATTEMPTS + 1):
            await run_notifications(
                factory, _settings(), now=NOW + timedelta(minutes=i), client=client
            )

    rows = await _deliveries(session)
    good = [r for r in rows if r.status == "sent"]
    bad = [r for r in rows if r.status == "failed"]
    assert len(good) == 1  # not re-sent while the other channel retries
    assert len(bad) == MAX_ATTEMPTS
    assert bad[0].error.startswith("HTTP 500")
    assert "s3cr3t" not in bad[0].error
    alert = await session.scalar(select(AlertEvent))
    # Given up after the last attempt, so the alert isn't rescanned forever.
    assert alert.notified_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=MAX_ATTEMPTS - 1)


async def test_retry_recovers(session, tenant_id, factory):
    session.add_all([_channel(tenant_id, "slack", SLACK), _alert(tenant_id)])
    await session.commit()
    rec = Recorder(status=503)
    async with rec.client() as client:
        assert await run_notifications(factory, _settings(), now=NOW, client=client) == 0
        alert = await session.scalar(select(AlertEvent))
        assert alert.notified_at is None
        rec.status = 200
        later = NOW + timedelta(minutes=5)
        assert await run_notifications(factory, _settings(), now=later, client=client) == 1
    assert [r.status for r in await _deliveries(session)] == ["failed", "sent"]


async def test_reraised_alert_is_sent_again(session, tenant_id, factory):
    session.add_all([_channel(tenant_id, "slack", SLACK), _alert(tenant_id)])
    await session.commit()
    rec = Recorder()
    async with rec.client() as client:
        await run_notifications(factory, _settings(), now=NOW, client=client)
        alert = await session.scalar(select(AlertEvent))
        alert.notified_at = None  # condition got worse: the raiser resets it
        alert.severity = "critical"
        await session.commit()
        later = NOW + timedelta(hours=1)
        assert await run_notifications(factory, _settings(), now=later, client=client) == 1
    assert len(rec.requests) == 2
    assert rec.bodies()[1]["text"].startswith("[CRITICAL]")


async def test_email_failure_recorded(session, tenant_id, factory, smtp):
    smtp.fail = True
    session.add_all([_channel(tenant_id, "email", "a@example.com"), _alert(tenant_id)])
    await session.commit()
    await run_notifications(factory, _settings(smtp_host="smtp.example.com"), now=NOW)
    (row,) = await _deliveries(session)
    assert row.status == "failed"
    assert "connection refused" in row.error


# --- digest ----------------------------------------------------------------------------


async def test_daily_digest_groups_by_severity(session, tenant_id, factory):
    session.add_all(
        [
            _channel(tenant_id, "slack", SLACK, digest=True, min_severity="info"),
            _channel(tenant_id, "webhook", HOOK, digest=True, signing_secret="k" * 20),
            _channel(tenant_id, "teams", TEAMS),  # not a digest channel
            _alert(tenant_id, "budget", "warning", "w1", message="Budget A at 85%"),
            _alert(tenant_id, "variance", "critical", "c1", message="Variance 40%"),
            _alert(tenant_id, "access_lost", "info", "i1", message="Lost access"),
            _alert(tenant_id, "budget", "critical", "old", created_at=NOW - timedelta(days=3)),
        ]
    )
    await session.commit()
    rec = Recorder()
    async with rec.client() as client:
        assert await run_daily_digest(factory, _settings(), now=NOW, client=client) == 2
        # Idempotent: rerunning the same day sends nothing.
        assert await run_daily_digest(factory, _settings(), now=NOW, client=client) == 0
    by_url = {str(r.url): json.loads(r.content) for r in rec.requests}
    text = by_url[SLACK]["text"]
    assert text.startswith("[CRITICAL] Daily alert digest")
    blocks = json.dumps(by_url[SLACK]["blocks"])
    assert blocks.index("CRITICAL (1)") < blocks.index("WARNING (1)") < blocks.index("INFO (1)")
    assert "Variance 40%" in blocks and "Lost access" in blocks
    # The webhook channel's default min_severity (warning) drops the info alert.
    event = by_url[HOOK]["event"]
    assert event["kind"] == "digest"
    assert event["counts"] == {"info": 0, "warning": 1, "critical": 1}
    assert [a["subject"] for a in event["alerts"]] == ["c1", "w1"]
    rows = await _deliveries(session)
    assert len(rows) == 2 and all(r.alert_event_id is None for r in rows)


async def test_digest_skipped_when_empty(session, tenant_id, factory):
    session.add_all(
        [
            _channel(tenant_id, "slack", SLACK, digest=True),
            _alert(tenant_id, created_at=NOW - timedelta(days=2)),
        ]
    )
    await session.commit()
    rec = Recorder()
    async with rec.client() as client:
        assert await run_daily_digest(factory, _settings(), now=NOW, client=client) == 0
    assert rec.requests == []
    assert await _deliveries(session) == []


async def test_jobs_without_tenant_do_nothing(factory):
    assert await run_notifications(factory, _settings(), now=NOW) == 0
    assert await run_daily_digest(factory, _settings(), now=NOW) == 0
