"""Channel-independent message content; each sender renders it in its own format."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ai_cost_tracker.notify.channels import SEVERITIES, severity_rank

_PREFIX = {"info": "[INFO]", "warning": "[WARNING]", "critical": "[CRITICAL]"}
MAX_FACTS = 8
MAX_VALUE = 200


@dataclass
class Message:
    title: str
    text: str
    severity: str
    link: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    # JSON body for generic webhooks (``event`` key); kept structured so receivers can parse it.
    event: dict[str, Any] = field(default_factory=dict)


def alerts_link(web_base_url: str) -> str:
    return f"{web_base_url.rstrip('/')}/alerts"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def alert_event_dict(alert: Any) -> dict[str, Any]:
    """The alert fields a webhook receiver gets (no tenant id: it's internal)."""
    return {
        "id": str(alert.id) if alert.id is not None else None,
        "kind": alert.kind,
        "severity": alert.severity or "warning",
        "subject": alert.subject,
        "message": alert.message,
        "details": alert.details or {},
        "created_at": _iso(alert.created_at),
        "resolved_at": _iso(alert.resolved_at),
    }


def alert_message(alert: Any, web_base_url: str) -> Message:
    """One alert: severity-prefixed title, the alert message and its key details."""
    severity = alert.severity or "warning"
    facts = [("Kind", alert.kind), ("Subject", _short(alert.subject))]
    for key, value in list((alert.details or {}).items())[:MAX_FACTS]:
        facts.append((str(key), _short(value)))
    return Message(
        title=f"{_PREFIX.get(severity, '[ALERT]')} {alert.kind.replace('_', ' ').capitalize()}",
        text=alert.message,
        severity=severity,
        link=alerts_link(web_base_url),
        facts=facts,
        event=alert_event_dict(alert),
    )


def digest_message(alerts: list[Any], web_base_url: str, *, now: datetime) -> Message:
    """One summary of many alerts, most severe group first."""
    groups: dict[str, list[Any]] = {}
    for alert in alerts:
        groups.setdefault(alert.severity or "warning", []).append(alert)
    order = sorted(groups, key=severity_rank, reverse=True)
    worst = order[0] if order else "info"
    lines = [f"{len(alerts)} alert(s) in the last 24 hours."]
    facts = []
    for severity in order:
        items = groups[severity]
        facts.append((severity.capitalize(), str(len(items))))
        lines.append("")
        lines.append(f"{severity.upper()} ({len(items)})")
        lines.extend(f"- {a.message}" for a in items)
    return Message(
        title=f"{_PREFIX.get(worst, '[INFO]')} Daily alert digest",
        text="\n".join(lines),
        severity=worst,
        link=alerts_link(web_base_url),
        facts=facts,
        event={
            "kind": "digest",
            "severity": worst,
            "generated_at": now.isoformat(),
            "counts": {s: len(groups.get(s, [])) for s in SEVERITIES},
            "alerts": [alert_event_dict(a) for a in alerts],
        },
    )
