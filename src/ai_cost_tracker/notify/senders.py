"""Render a ``Message`` per channel kind and deliver it.

Webhook URLs embed their own credentials, so they never appear in logs, errors or API
responses: errors are scrubbed with ``mask_target`` before they leave this module.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx

from ai_cost_tracker.config import Settings, get_settings
from ai_cost_tracker.notify.channels import mask_target, validate_target
from ai_cost_tracker.notify.message import Message, alert_message

log = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10.0
SMTP_TIMEOUT_SECONDS = 15.0
MAX_ERROR = 500


class NotifyError(Exception):
    """A delivery failed; the message is safe to store and show (no secrets)."""


def make_client() -> httpx.AsyncClient:
    """HTTP client for webhook calls. Tests monkeypatch this to inject a MockTransport."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)


# --- payloads --------------------------------------------------------------------------


def slack_payload(msg: Message) -> dict[str, Any]:
    """Incoming-webhook body: ``text`` is the notification fallback, blocks the rich view."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{msg.title}*\n{msg.text}"}},
    ]
    if msg.facts:
        # Slack allows at most 10 fields per section.
        fields = [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in msg.facts[:10]]
        blocks.append({"type": "section", "fields": fields})
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{msg.link}|Open alerts>"}]}
    )
    return {"text": f"{msg.title}: {msg.text}"[:3000], "blocks": blocks}


_TEAMS_COLOUR = {"critical": "Attention", "warning": "Warning", "info": "Default"}


def teams_payload(msg: Message) -> dict[str, Any]:
    """Adaptive Card wrapped as a message, the shape Teams "Workflows" webhooks take.

    Office 365 connector (MessageCard) webhooks are retired. Unverified: that every
    Workflows template accepts this exact envelope; the "Post to a channel when a webhook
    request is received" template is documented to.
    """
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": msg.title,
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
                "color": _TEAMS_COLOUR.get(msg.severity, "Default"),
            },
            {"type": "TextBlock", "text": msg.text, "wrap": True},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Open alerts", "url": msg.link}],
    }
    if msg.facts:
        card["body"].append(
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in msg.facts]}
        )
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """``X-ACT-Signature`` value: HMAC-SHA256 of ``"<X-ACT-Timestamp>." + raw body``.

    Signing the timestamp too lets receivers reject replays of an old, captured request
    (check the timestamp is recent, then the signature).
    """
    signed = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def webhook_request(msg: Message, secret: str | None, *, now: datetime) -> tuple[bytes, dict]:
    """Body and headers for a generic webhook; see ``sign`` for how receivers verify it."""
    body = json.dumps(
        {"event": msg.event, "sent_at": now.isoformat()}, separators=(",", ":"), default=str
    ).encode()
    timestamp = str(int(now.timestamp()))
    headers = {"Content-Type": "application/json", "X-ACT-Timestamp": timestamp}
    if secret:
        headers["X-ACT-Signature"] = sign(secret, timestamp, body)
    return body, headers


def email_message(msg: Message, to: str, sender: str) -> EmailMessage:
    email = EmailMessage()
    email["Subject"] = msg.title
    email["From"] = sender
    email["To"] = to
    parts = [msg.text]
    if msg.facts:
        parts.append("\n".join(f"{k}: {v}" for k, v in msg.facts))
    parts.append(f"Open alerts: {msg.link}")
    email.set_content("\n\n".join(parts) + "\n")
    return email


# --- delivery --------------------------------------------------------------------------


def _scrub(text: str, target: str, kind: str) -> str:
    return text.replace(target, mask_target(kind, target))[:MAX_ERROR]


async def _post(
    client: httpx.AsyncClient, kind: str, url: str, *, content: bytes, headers: dict
) -> None:
    try:
        resp = await client.post(url, content=content, headers=headers)
    except httpx.HTTPError as exc:
        raise NotifyError(_scrub(f"{type(exc).__name__}: {exc}", url, kind)) from None
    if resp.status_code >= 300:
        raise NotifyError(_scrub(f"HTTP {resp.status_code}: {resp.text[:200]}", url, kind))


def _send_email_sync(settings: Settings, to: str, email: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_username:
            s.login(settings.smtp_username, settings.smtp_password)
        s.send_message(email, to_addrs=[to])


async def send_email(settings: Settings, to: str, msg: Message) -> None:
    if not settings.smtp_host:
        raise NotifyError("Email is disabled: ACT_SMTP_HOST is not set")
    email = email_message(msg, to, settings.smtp_from)
    try:
        await asyncio.to_thread(_send_email_sync, settings, to, email)
    except (OSError, smtplib.SMTPException) as exc:
        # Never echo the password; smtplib errors don't include it, but keep it short anyway.
        raise NotifyError(f"SMTP {type(exc).__name__}: {str(exc)[:200]}") from None


async def deliver(
    channel: Any,
    msg: Message,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> None:
    """Send ``msg`` to one channel; raises ``NotifyError`` with a safe reason on failure."""
    now = now or datetime.now(UTC)
    kind, target = channel.kind, channel.target
    if kind == "email":
        await send_email(settings, target, msg)
        return
    try:
        validate_target(kind, target, environment=settings.environment)
    except ValueError as exc:
        raise NotifyError(str(exc)) from None
    if kind == "slack":
        content, headers = json.dumps(slack_payload(msg)).encode(), {}
    elif kind == "teams":
        content, headers = json.dumps(teams_payload(msg)).encode(), {}
    elif kind == "webhook":
        content, headers = webhook_request(msg, channel.signing_secret, now=now)
    else:
        raise NotifyError(f"Unknown channel kind {kind!r}")
    headers.setdefault("Content-Type", "application/json")
    if client is not None:
        await _post(client, kind, target, content=content, headers=headers)
        return
    async with make_client() as own:
        await _post(own, kind, target, content=content, headers=headers)


class _TestAlert:
    """A synthetic alert so "send test" exercises the real rendering path."""

    def __init__(self, now: datetime) -> None:
        self.id = None
        self.kind = "test"
        self.severity = "info"
        self.subject = "test"
        self.message = "Test notification from AI Cost Tracker. This channel is set up correctly."
        self.details = {"note": "No action needed"}
        self.created_at = now
        self.resolved_at = None


async def send_test(
    channel: Any,
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> None:
    """Send a synthetic alert to ``channel``; raises ``NotifyError`` on failure."""
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    msg = alert_message(_TestAlert(now), settings.web_base_url)
    await deliver(channel, msg, settings, client=client, now=now)
    log.info(
        "test notification sent to %s channel %s",
        channel.kind,
        mask_target(channel.kind, channel.target),
    )
