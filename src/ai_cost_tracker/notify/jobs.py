"""Notification jobs: per-alert delivery with retries, and the daily digest.

Plain async functions; the worker schedules them (no arq imports here).

Retry model: each run attempts every matching channel that hasn't succeeded yet for the
alert's current "round" and records one ``NotificationDelivery`` per attempt. A channel that
fails ``MAX_ATTEMPTS`` times in a round is given up on. ``notified_at`` is set once every
matching channel has succeeded or been given up on (or immediately when none match), so the
job never rescans a handled alert. When an alert is re-raised (``notified_at`` reset to
null) a new round starts; rounds are recovered from delivery history (see ``_round_state``).
"""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import groupby

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import AlertEvent, NotificationChannel, NotificationDelivery, Tenant
from ai_cost_tracker.notify.channels import mask_target, matches, severity_rank
from ai_cost_tracker.notify.message import alert_message, digest_message
from ai_cost_tracker.notify.senders import NotifyError, deliver, make_client

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BATCH = 200
DIGEST_WINDOW = timedelta(hours=24)
# A digest already sent within this window counts as today's, so a rerun doesn't duplicate it.
DIGEST_DEDUPE = timedelta(hours=23)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass
class _PairState:
    sent: bool = False
    failures: int = 0

    @property
    def terminal(self) -> bool:
        return self.sent or self.failures >= MAX_ATTEMPTS


def _round_state(
    history: Iterable[NotificationDelivery], channel_ids: set[uuid.UUID]
) -> dict[uuid.UUID, _PairState]:
    """Per-channel state of the alert's current round, replayed from its deliveries.

    One run stamps all its attempts with the same ``attempted_at``, so deliveries group into
    runs. A round ends after the run in which every channel in it became terminal; the job
    finalises the alert in that same run. If the latest round is complete but the alert is
    pending again, it was re-raised, so we start fresh. Edge case: a channel deactivated
    while it still had retries pending can let one completed channel be sent twice.
    """
    state: dict[uuid.UUID, _PairState] = {}
    ordered = sorted(
        (d for d in history if d.channel_id in channel_ids),
        key=lambda d: (_utc(d.attempted_at), d.id or 0),
    )
    for _, run in groupby(ordered, key=lambda d: _utc(d.attempted_at)):
        for d in run:
            pair = state.setdefault(d.channel_id, _PairState())
            if d.status == "sent":
                pair.sent = True
            else:
                pair.failures += 1
        if state and all(p.terminal for p in state.values()):
            state = {}
    return state


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC so comparisons work on both."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _tenant_id(session: AsyncSession, settings: Settings) -> uuid.UUID | None:
    return await session.scalar(
        select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
    )


async def _channels(session: AsyncSession, tenant_id: uuid.UUID, *, digest: bool) -> list:
    rows = await session.scalars(
        select(NotificationChannel)
        .where(
            NotificationChannel.tenant_id == tenant_id,
            NotificationChannel.active.is_(True),
            NotificationChannel.digest.is_(digest),
        )
        .order_by(NotificationChannel.created_at, NotificationChannel.id)
    )
    return list(rows)


async def run_notifications(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Deliver pending alerts to matching non-digest channels. Returns messages sent."""
    now = now or datetime.now(UTC)
    sent = 0
    async with session_factory() as session:
        tenant_id = await _tenant_id(session, settings)
        if tenant_id is None:
            return 0
        alerts = list(
            await session.scalars(
                select(AlertEvent)
                .where(
                    AlertEvent.tenant_id == tenant_id,
                    AlertEvent.notified_at.is_(None),
                    AlertEvent.resolved_at.is_(None),
                )
                .order_by(AlertEvent.id)
                .limit(BATCH)
            )
        )
        if not alerts:
            return 0
        channels = await _channels(session, tenant_id, digest=False)
        history = list(
            await session.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.tenant_id == tenant_id,
                    NotificationDelivery.alert_event_id.in_([a.id for a in alerts]),
                )
            )
        )
        own = client or make_client()
        try:
            for alert in alerts:
                sent += await _notify_one(session, settings, alert, channels, history, own, now)
                # Commit per alert: a crash later in the batch must not resend this one.
                await session.commit()
        finally:
            if client is None:
                await own.aclose()
    return sent


async def _notify_one(
    session: AsyncSession,
    settings: Settings,
    alert: AlertEvent,
    channels: list,
    history: list[NotificationDelivery],
    client: httpx.AsyncClient,
    now: datetime,
) -> int:
    targets = [c for c in channels if matches(c, alert.kind, alert.severity)]
    state = _round_state(
        (d for d in history if d.alert_event_id == alert.id), {c.id for c in targets}
    )
    msg = alert_message(alert, settings.web_base_url)
    sent = 0
    finished = True
    for channel in targets:
        pair = state.setdefault(channel.id, _PairState())
        if pair.terminal:
            continue
        error = None
        try:
            await deliver(channel, msg, settings, client=client, now=now)
            pair.sent = True
            sent += 1
        except NotifyError as exc:
            error = str(exc)
            pair.failures += 1
            masked = mask_target(channel.kind, channel.target)
            if pair.terminal:
                log.warning(
                    "giving up on alert %s for %s channel %s after %d attempts: %s",
                    alert.id,
                    channel.kind,
                    masked,
                    pair.failures,
                    error,
                )
            else:
                finished = False
                log.info("alert %s to %s failed, will retry: %s", alert.id, masked, error)
        session.add(
            NotificationDelivery(
                tenant_id=alert.tenant_id,
                channel_id=channel.id,
                alert_event_id=alert.id,
                status="sent" if error is None else "failed",
                error=error,
                attempted_at=now,
            )
        )
    if finished:
        alert.notified_at = now
    return sent


async def run_daily_digest(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """One summary per digest channel of the last 24 h of alerts. Returns digests sent.

    Skips channels with nothing to report, and channels that already got a digest within
    ``DIGEST_DEDUPE`` so a rerun on the same day doesn't send twice.
    """
    now = now or datetime.now(UTC)
    since = now - DIGEST_WINDOW
    sent = 0
    async with session_factory() as session:
        tenant_id = await _tenant_id(session, settings)
        if tenant_id is None:
            return 0
        channels = await _channels(session, tenant_id, digest=True)
        if not channels:
            return 0
        # notified_at catches re-raised alerts, whose created_at is older than the window.
        alerts = list(
            await session.scalars(
                select(AlertEvent)
                .where(
                    AlertEvent.tenant_id == tenant_id,
                    or_(AlertEvent.created_at >= since, AlertEvent.notified_at >= since),
                )
                .order_by(AlertEvent.created_at, AlertEvent.id)
            )
        )
        recent = set(
            await session.scalars(
                select(NotificationDelivery.channel_id).where(
                    NotificationDelivery.tenant_id == tenant_id,
                    NotificationDelivery.alert_event_id.is_(None),
                    NotificationDelivery.status == "sent",
                    NotificationDelivery.attempted_at > now - DIGEST_DEDUPE,
                )
            )
        )
        own = client or make_client()
        try:
            for channel in channels:
                if channel.id in recent:
                    continue
                picked = [a for a in alerts if matches(channel, a.kind, a.severity)]
                if not picked:
                    continue
                picked.sort(key=lambda a: -severity_rank(a.severity))
                msg = digest_message(picked, settings.web_base_url, now=now)
                error = None
                try:
                    await deliver(channel, msg, settings, client=own, now=now)
                    sent += 1
                except NotifyError as exc:
                    error = str(exc)
                    log.warning(
                        "digest to %s channel %s failed: %s",
                        channel.kind,
                        mask_target(channel.kind, channel.target),
                        error,
                    )
                session.add(
                    NotificationDelivery(
                        tenant_id=tenant_id,
                        channel_id=channel.id,
                        alert_event_id=None,
                        status="sent" if error is None else "failed",
                        error=error,
                        attempted_at=now,
                    )
                )
                await session.commit()
        finally:
            if client is None:
                await own.aclose()
    return sent
