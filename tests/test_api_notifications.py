"""Notification channel API: CRUD, masking, one-time secrets, test sends, roles and audit."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai_cost_tracker.api.app import create_app
from ai_cost_tracker.api.auth import Principal, get_principal
from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.db.models import (
    AlertEvent,
    AuditLog,
    NotificationChannel,
    NotificationDelivery,
    Tenant,
)
from ai_cost_tracker.db.session import get_session
from ai_cost_tracker.notify import senders

ADMIN = Principal(oid="a1", name="Admin", roles={"admin"})
VIEWER = Principal(oid="v1", name="Viewer", roles={"viewer"})
URL = "/api/v1/notifications/channels"
SLACK = "https://hooks.slack.com/services/T000/B000/secrettokenabcd"
HOOK = "https://example.com/hooks/act?token=zzzz1234"


def _settings() -> Settings:
    return Settings(environment="test", default_tenant_slug="internal")


class State:
    principal = ADMIN
    status = 200
    requests: list[httpx.Request] = []


@pytest.fixture
def state(monkeypatch):
    s = State()
    s.requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        s.requests.append(request)
        return httpx.Response(s.status, text="ok" if s.status < 300 else "invalid_token")

    monkeypatch.setattr(
        senders, "make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return s


@pytest.fixture
def app(engine, state):
    app = create_app(_settings())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_principal] = lambda: state.principal
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _audit(session) -> list[AuditLog]:
    session.expire_all()
    return list(await session.scalars(select(AuditLog).order_by(AuditLog.id)))


async def test_create_list_masks_target(client, session, tenant_id):
    resp = await client.post(
        URL,
        json={"name": "FinOps Slack", "kind": "slack", "target": SLACK, "alert_kinds": ["budget"]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_masked"] == "https://hooks.slack.com/…/abcd"
    assert "signing_secret" not in body
    assert "secrettoken" not in resp.text

    listed = (await client.get(URL)).json()
    assert listed == [
        {
            "id": body["id"],
            "name": "FinOps Slack",
            "kind": "slack",
            "target_masked": "https://hooks.slack.com/…/abcd",
            "alert_kinds": ["budget"],
            "min_severity": "warning",
            "digest": False,
            "active": True,
        }
    ]
    stored = await session.scalar(select(NotificationChannel))
    assert stored.target == SLACK and stored.tenant_id == tenant_id

    (row,) = await _audit(session)
    assert row.action == "notification_channel.create"
    assert row.target_id == body["id"]
    assert "secrettoken" not in json.dumps(row.details)


async def test_webhook_secret_generated_and_shown_once(client, session, tenant_id):
    resp = await client.post(URL, json={"name": "Hook", "kind": "webhook", "target": HOOK})
    assert resp.status_code == 201
    secret = resp.json()["signing_secret"]
    assert len(secret) >= 32
    assert "signing_secret" not in (await client.get(URL)).json()[0]
    stored = await session.scalar(select(NotificationChannel))
    assert stored.signing_secret == secret
    for row in await _audit(session):
        assert secret not in json.dumps(row.details)
        assert "zzzz1234" not in json.dumps(row.details)

    # A caller-supplied secret is stored but never echoed.
    resp = await client.post(
        URL, json={"name": "Hook 2", "kind": "webhook", "target": HOOK, "signing_secret": "m" * 32}
    )
    assert "signing_secret" not in resp.json()


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("email", "not-an-email"),
        ("slack", "http://hooks.slack.com/x"),
        ("teams", "javascript:alert(1)"),
        ("webhook", "https://u:p@example.com/x"),
        ("pager", "https://example.com"),
    ],
)
async def test_invalid_targets_rejected(client, tenant_id, kind, target):
    resp = await client.post(URL, json={"name": "x", "kind": kind, "target": target})
    assert resp.status_code == 422


async def test_update_partial_and_delete(client, session, tenant_id):
    created = (
        await client.post(URL, json={"name": "Mail", "kind": "email", "target": "a@example.com"})
    ).json()
    resp = await client.put(
        f"{URL}/{created['id']}", json={"min_severity": "critical", "digest": True}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["min_severity"] == "critical" and body["digest"] is True
    assert body["target_masked"] == "a…@example.com"

    # Switching kind revalidates the existing target.
    resp = await client.put(f"{URL}/{created['id']}", json={"kind": "slack"})
    assert resp.status_code == 422

    resp = await client.put(f"{URL}/{created['id']}", json={"kind": "webhook", "target": HOOK})
    assert resp.status_code == 200
    assert "signing_secret" in resp.json()

    stored = await session.scalar(select(NotificationChannel))
    session.add(
        NotificationDelivery(
            tenant_id=tenant_id,
            channel_id=stored.id,
            status="sent",
            attempted_at=datetime.now(UTC),
        )
    )
    await session.commit()
    assert (await client.delete(f"{URL}/{created['id']}")).status_code == 204
    assert (await client.get(URL)).json() == []
    assert (await client.delete(f"{URL}/{created['id']}")).status_code == 404

    actions = [r.action for r in await _audit(session)]
    assert actions == [
        "notification_channel.create",
        "notification_channel.update",
        "notification_channel.update",
        "notification_channel.delete",
    ]
    update = (await _audit(session))[2]
    assert update.details["target_changed"] is True
    assert "zzzz" not in json.dumps(update.details)


async def test_test_endpoint_sent_and_failed(client, session, tenant_id, state):
    created = (await client.post(URL, json={"name": "S", "kind": "slack", "target": SLACK})).json()
    resp = await client.post(f"{URL}/{created['id']}/test")
    assert resp.status_code == 200
    assert resp.json() == {"status": "sent"}
    payload = json.loads(state.requests[0].content)
    assert payload["text"].startswith("[INFO]")

    state.status = 403
    resp = await client.post(f"{URL}/{created['id']}/test")
    assert resp.status_code == 502
    assert "HTTP 403" in resp.json()["detail"]
    assert "secrettoken" not in resp.text

    tests = [r for r in await _audit(session) if r.action == "notification_channel.test"]
    assert [r.details["status"] for r in tests] == ["sent", "failed"]


async def test_test_email_disabled_returns_502(client, tenant_id):
    created = (
        await client.post(URL, json={"name": "M", "kind": "email", "target": "a@example.com"})
    ).json()
    resp = await client.post(f"{URL}/{created['id']}/test")
    assert resp.status_code == 502
    assert "ACT_SMTP_HOST" in resp.json()["detail"]


async def test_viewer_can_read_but_not_change(client, session, tenant_id, state):
    created = (await client.post(URL, json={"name": "S", "kind": "slack", "target": SLACK})).json()
    state.principal = VIEWER
    assert (await client.get(URL)).status_code == 200
    assert (await client.get("/api/v1/notifications/deliveries")).status_code == 200
    assert (
        await client.post(URL, json={"name": "x", "kind": "slack", "target": SLACK})
    ).status_code == 403
    assert (await client.put(f"{URL}/{created['id']}", json={"name": "y"})).status_code == 403
    assert (await client.delete(f"{URL}/{created['id']}")).status_code == 403
    assert (await client.post(f"{URL}/{created['id']}/test")).status_code == 403
    assert state.requests == []


async def test_deliveries_list_is_tenant_scoped(client, session, tenant_id):
    other = Tenant(slug="other", name="Other")
    session.add(other)
    await session.flush()
    mine = NotificationChannel(tenant_id=tenant_id, name="Mine", kind="slack", target=SLACK)
    theirs = NotificationChannel(tenant_id=other.id, name="Theirs", kind="slack", target=SLACK)
    alert = AlertEvent(tenant_id=tenant_id, kind="budget", subject="b", message="m")
    session.add_all([mine, theirs, alert])
    await session.flush()
    t1 = datetime(2026, 10, 1, 8, tzinfo=UTC)
    t2 = datetime(2026, 10, 1, 9, tzinfo=UTC)
    session.add_all(
        [
            NotificationDelivery(
                tenant_id=tenant_id,
                channel_id=mine.id,
                alert_event_id=alert.id,
                status="failed",
                error="HTTP 500",
                attempted_at=t1,
            ),
            NotificationDelivery(
                tenant_id=tenant_id, channel_id=mine.id, status="sent", attempted_at=t2
            ),
            NotificationDelivery(
                tenant_id=other.id, channel_id=theirs.id, status="sent", attempted_at=t2
            ),
        ]
    )
    await session.commit()
    resp = await client.get("/api/v1/notifications/deliveries", params={"limit": 10})
    rows = resp.json()
    assert [r["status"] for r in rows] == ["sent", "failed"]
    assert rows[0]["alert_event_id"] is None
    assert rows[1] == {
        "id": rows[1]["id"],
        "channel_id": str(mine.id),
        "channel_name": "Mine",
        "alert_event_id": str(alert.id),
        "status": "failed",
        "error": "HTTP 500",
        "attempted_at": "2026-10-01T08:00:00+00:00",
    }
    assert (await client.get("/api/v1/notifications/deliveries?limit=0")).status_code == 422


async def test_create_without_tenant_creates_it(client, session):
    resp = await client.post(URL, json={"name": "S", "kind": "slack", "target": SLACK})
    assert resp.status_code == 201
    tenant = await session.scalar(select(Tenant).where(Tenant.slug == "internal"))
    assert tenant is not None
