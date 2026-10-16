"""Channel rules shared by the API and the jobs: validation, masking and alert matching."""

import re
from typing import Protocol
from urllib.parse import urlsplit

KINDS = ("email", "slack", "teams", "webhook")
SEVERITIES = ("info", "warning", "critical")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# Deliberately simple: one @, no spaces, a dot in the domain. The SMTP server is the real judge.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChannelLike(Protocol):
    kind: str
    alert_kinds: list
    min_severity: str


def severity_rank(severity: str | None) -> int:
    """Unknown severities rank as "warning", the column default."""
    return _RANK.get(severity or "warning", _RANK["warning"])


def matches(channel: ChannelLike, kind: str, severity: str | None) -> bool:
    """Empty ``alert_kinds`` means every kind."""
    if channel.alert_kinds and kind not in channel.alert_kinds:
        return False
    return severity_rank(severity) >= severity_rank(channel.min_severity)


def validate_target(kind: str, target: str, *, environment: str) -> str:
    """Return the cleaned target or raise ``ValueError`` with a user-facing reason.

    Webhook URLs carry their own credentials, so only https is accepted (plain http only for
    local development) and URLs with embedded user info are refused.
    """
    target = target.strip()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "email":
        if not _EMAIL.match(target):
            raise ValueError("target must be an email address")
        return target
    parts = urlsplit(target)
    allowed = {"https", "http"} if environment == "local" else {"https"}
    if parts.scheme not in allowed or not parts.hostname:
        raise ValueError("target must be an https:// URL")
    if parts.username or parts.password:
        raise ValueError("target URL must not contain credentials")
    return target


def mask_target(kind: str, target: str) -> str:
    """Enough to recognise a channel, not enough to use it: scheme, host and last 4 chars."""
    if kind == "email":
        local, _, domain = target.partition("@")
        return f"{local[:1]}…@{domain}" if domain else "…"
    parts = urlsplit(target)
    tail = target[-4:] if len(target) > 8 else ""
    if not parts.scheme or not parts.hostname:
        return f"…{tail}"
    return f"{parts.scheme}://{parts.hostname}/…/{tail}"
