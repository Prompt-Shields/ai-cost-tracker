"""PTU utilisation ingest and the idle-capacity report (issue #32).

Provisioned (PTU) deployments are billed per reserved hour whatever the traffic, so the
question is how much of that capacity is used. Hourly utilisation comes from the connector
(Azure Monitor); cost comes from billed cost lines.

Report maths, per provisioned deployment and day range:
- ``avg_utilization`` / ``peak_utilization``: mean and maximum of the stored hourly averages
  (the hourly Maximum aggregation isn't stored, so "peak" is the busiest hour's average);
- ``idle_hours``: sampled hours with utilisation below ``IDLE_THRESHOLD``; ``hours`` counts
  sampled hours only, so an hour Monitor leaves out is "no data", not idle (unverified, #2);
- ``ptu_cost``: billed cost lines on the deployment's resource whose ``ai_direction`` is
  "provisioned" or whose meter contains "Provisioned" (PTU meters name no model, so they are
  usually unmapped). Several PTU deployments on one resource split it by capacity (equally
  when capacities are unknown);
- ``idle_cost = ptu_cost x (1 - avg_utilization)``: the share of spend that bought unused
  capacity. Null when there is no utilisation data.

Reservation purchases are listed separately, never mixed into ``ptu_cost``. Heuristic
(unverified, #2): a cost line whose meter or service name contains "reservation" or
"reserved". Two known gaps: hours covered by a reservation bill PTU meters at 0 in actual
cost, so their ``ptu_cost`` (and idle cost) reads 0 until amortised cost is ingested; and the
cost query filters to AI resource types, so purchases (on a reservation order, not the
account) only show once cost ingestion also pulls them.
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import delete, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors import Connector, get_connector
from ai_cost_tracker.connectors.base import is_provisioned
from ai_cost_tracker.core.models import Deployment
from ai_cost_tracker.db.models import (
    CloudAccountRow,
    CostLine,
    DeploymentRow,
    PtuUtilization,
    Tenant,
)

log = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# Scheduled pass: the last two hours plus the current partial one, like token usage.
PTU_WINDOW = timedelta(hours=2)
BACKFILL_DAYS = 30
BACKFILL_CHUNK = timedelta(days=7)
IDLE_THRESHOLD = Decimal("0.10")
DEFAULT_CURRENCY = "USD"

_UTIL_PLACES = Decimal("0.0001")  # matches Numeric(7, 4)
_MONEY_PLACES = Decimal("0.0000000001")  # matches Numeric(20, 10)
_RESERVATION_WORDS = ("reservation", "reserved")


# --- ingest ----------------------------------------------------------------------------


def _hour(value: datetime) -> datetime:
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.replace(minute=0, second=0, microsecond=0)


def ptu_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Hour-aligned ``[start, end)``: the last two hours and the current one."""
    hour = _hour(now or datetime.now(UTC))
    return hour - PTU_WINDOW, hour + timedelta(hours=1)


async def ingest_ptu(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connector: Connector,
    account_id: str,
    deployments: list[Deployment],
    start: datetime,
    end: datetime,
    cloud: str = "azure",
) -> int:
    """Replace stored utilisation of the account's PTU deployments for ``[start, end)``.

    The partition is (resource, deployment, hour): rows in the window are deleted and the
    connector's answer inserted, so re-running leaves identical rows.
    """
    ptu = [d for d in deployments if is_provisioned(d.sku)]
    if not ptu:
        return 0
    start = _hour(start)
    end = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
    samples = await connector.pull_ptu_utilization(account_id, ptu, start, end)
    pairs = sorted({(d.resource_id, d.name) for d in ptu})
    await session.execute(
        delete(PtuUtilization).where(
            PtuUtilization.tenant_id == tenant_id,
            PtuUtilization.cloud == cloud,
            tuple_(PtuUtilization.resource_id, PtuUtilization.deployment).in_(pairs),
            PtuUtilization.bucket_start >= start,
            PtuUtilization.bucket_start < end,
        )
    )
    wanted = set(pairs)
    rows: dict[tuple[str, str, datetime], PtuUtilization] = {}
    for s in samples:
        bucket = _hour(s.bucket_start)
        if (s.resource_id, s.deployment) not in wanted or not start <= bucket < end:
            continue
        rows[(s.resource_id, s.deployment, bucket)] = PtuUtilization(
            tenant_id=tenant_id,
            cloud=cloud,
            account_id=account_id,
            resource_id=s.resource_id,
            deployment=s.deployment,
            bucket_start=bucket,
            utilization=s.utilization.quantize(_UTIL_PLACES),
        )
    session.add_all(rows.values())
    await session.commit()
    return len(rows)


@dataclass
class _Account:
    cloud: str
    account_id: str
    deployments: list[Deployment] = field(default_factory=list)


async def _ptu_accounts(
    session_factory: SessionFactory, tenant_id: uuid.UUID, account_id: str | None
) -> list[_Account]:
    """Tracked, connected accounts that have live provisioned deployments."""
    async with session_factory() as session:
        where = [
            CloudAccountRow.tenant_id == tenant_id,
            CloudAccountRow.tracked.is_(True),
            CloudAccountRow.access_status == "connected",
        ]
        if account_id is not None:
            where.append(CloudAccountRow.account_id == account_id)
        accounts = {
            (c, a): _Account(c, a)
            for c, a in await session.execute(
                select(CloudAccountRow.cloud, CloudAccountRow.account_id).where(*where)
            )
        }
        if not accounts:
            return []
        rows = await session.scalars(
            select(DeploymentRow).where(
                DeploymentRow.tenant_id == tenant_id,
                DeploymentRow.deleted_at.is_(None),
                DeploymentRow.account_id.in_([a for _, a in accounts]),
            )
        )
        for r in rows:
            acct = accounts.get((r.cloud, r.account_id))
            if acct is not None and is_provisioned(r.sku):
                acct.deployments.append(_deployment(r))
    return [a for _, a in sorted(accounts.items()) if a.deployments]


def _deployment(r: DeploymentRow) -> Deployment:
    return Deployment(
        cloud=r.cloud,
        account_id=r.account_id,
        resource_id=r.resource_id,
        name=r.name,
        model=r.model,
        model_version=r.model_version,
        sku=r.sku,
        capacity=r.capacity,
        region=r.region,
        tags=r.tags or {},
    )


async def _tenant(session_factory: SessionFactory, settings: Settings) -> uuid.UUID | None:
    async with session_factory() as session:
        return await session.scalar(
            select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
        )


async def _ingest_range(
    session_factory: SessionFactory,
    settings: Settings,
    start: datetime,
    end: datetime,
    connector: Connector | None,
    account_id: str | None = None,
) -> int:
    tenant_id = await _tenant(session_factory, settings)
    if tenant_id is None:
        return 0
    accounts = await _ptu_accounts(session_factory, tenant_id, account_id)
    if not accounts:
        return 0
    own = connector is None
    conn = connector or get_connector(settings)
    total = 0
    try:
        for acct in accounts:
            chunk = _hour(start)
            while chunk < end:
                stop = min(chunk + BACKFILL_CHUNK, end)
                try:
                    async with session_factory() as session:
                        total += await ingest_ptu(
                            session,
                            tenant_id,
                            conn,
                            acct.account_id,
                            acct.deployments,
                            chunk,
                            stop,
                            acct.cloud,
                        )
                except Exception:
                    # One account (or chunk) failing must not stop the others.
                    log.exception("PTU ingest failed for %s %s..%s", acct.account_id, chunk, stop)
                chunk = stop
    finally:
        if own and hasattr(conn, "aclose"):
            await conn.aclose()
    return total


async def run_ptu(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
    connector: Connector | None = None,
) -> int:
    """Scheduled pass: the last two hours of utilisation for every tracked PTU deployment."""
    start, end = ptu_window(now)
    rows = await _ingest_range(session_factory, settings, start, end, connector)
    log.info("ptu: %d rows", rows)
    return rows


async def backfill_ptu(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    days: int = BACKFILL_DAYS,
    now: datetime | None = None,
    account_id: str | None = None,
    connector: Connector | None = None,
) -> int:
    """``days`` of hourly utilisation in 7-day chunks (e.g. after connecting an account)."""
    _start, end = ptu_window(now)
    start = end - timedelta(hours=1) - timedelta(days=days)
    rows = await _ingest_range(session_factory, settings, start, end, connector, account_id)
    log.info("ptu backfill (%d days): %d rows", days, rows)
    return rows


# --- report ----------------------------------------------------------------------------


@dataclass
class PtuDeploymentReport:
    resource_id: str
    account_id: str
    deployment: str
    model: str | None
    sku: str | None
    capacity: int | None
    avg_utilization: Decimal | None
    peak_utilization: Decimal | None
    idle_hours: int
    hours: int
    ptu_cost: Decimal
    idle_cost: Decimal | None
    currency: str


@dataclass
class ReservationPurchase:
    day: date
    account_id: str
    resource_id: str
    sku_meter: str
    service_name: str | None
    cost: Decimal
    currency: str


@dataclass
class PtuReport:
    currency: str
    deployments: list[PtuDeploymentReport]
    reservations: list[ReservationPurchase]


def deployment_key(resource_id: str, deployment: str) -> str:
    """``<account resource name>/<deployment>``, the id the API and UI use for a deployment."""
    resource = resource_id.rstrip("/").rsplit("/", 1)[-1] or resource_id
    return f"{resource}/{deployment}"


def looks_like_reservation(sku_meter: str, service_name: str | None) -> bool:
    text = f"{sku_meter} {service_name or ''}".lower()
    return any(w in text for w in _RESERVATION_WORDS)


def _is_ptu_charge(line: CostLine) -> bool:
    if looks_like_reservation(line.sku_meter, line.service_name):
        return False
    return line.ai_direction == "provisioned" or "provisioned" in line.sku_meter.lower()


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(start, time.min, tzinfo=UTC)
    return lo, datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)


def _dec(value: object) -> Decimal:
    # SQLite may hand back floats for numerics; go through str() to avoid binary noise.
    return Decimal(str(value or 0))


@dataclass
class _Dep:
    resource_id: str
    account_id: str
    deployment: str
    model: str | None = None
    sku: str | None = None
    capacity: int | None = None
    values: list[Decimal] = field(default_factory=list)


async def ptu_deployments(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> dict[tuple[str, str], _Dep]:
    """Provisioned deployments live in the range, plus any with samples in it."""
    lo, hi = _day_bounds(start, end)
    deps: dict[tuple[str, str], _Dep] = {}
    rows = await session.scalars(
        select(DeploymentRow).where(
            DeploymentRow.tenant_id == tenant_id,
            or_(DeploymentRow.deleted_at.is_(None), DeploymentRow.deleted_at >= lo),
        )
    )
    for r in rows:
        if is_provisioned(r.sku):
            deps[(r.resource_id, r.name)] = _Dep(
                r.resource_id, r.account_id, r.name, r.model, r.sku, r.capacity
            )
    samples = await session.execute(
        select(
            PtuUtilization.resource_id,
            PtuUtilization.account_id,
            PtuUtilization.deployment,
            PtuUtilization.utilization,
        ).where(
            PtuUtilization.tenant_id == tenant_id,
            PtuUtilization.bucket_start >= lo,
            PtuUtilization.bucket_start < hi,
        )
    )
    for resource_id, account_id, name, value in samples:
        dep = deps.get((resource_id, name))
        if dep is None:
            dep = deps[(resource_id, name)] = _Dep(resource_id, account_id, name)
        dep.values.append(_dec(value).quantize(_UTIL_PLACES))
    return deps


def _shares(deps: Iterable[_Dep]) -> dict[tuple[str, str], Decimal]:
    """Each deployment's share of its resource's PTU cost: by capacity, else equal."""
    by_resource: dict[str, list[_Dep]] = defaultdict(list)
    for d in deps:
        by_resource[d.resource_id.lower()].append(d)
    shares: dict[tuple[str, str], Decimal] = {}
    for group in by_resource.values():
        total = sum((d.capacity or 0) for d in group)
        for d in group:
            if total > 0:
                shares[(d.resource_id, d.deployment)] = Decimal(d.capacity or 0) / Decimal(total)
            else:
                shares[(d.resource_id, d.deployment)] = Decimal(1) / Decimal(len(group))
    return shares


async def ptu_report(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> PtuReport:
    """Utilisation, idle hours and wasted spend per PTU deployment for ``start..end``."""
    deps = await ptu_deployments(session, tenant_id, start, end)
    lines = list(
        await session.scalars(
            select(CostLine).where(
                CostLine.tenant_id == tenant_id,
                CostLine.charge_period_start >= start,
                CostLine.charge_period_start <= end,
            )
        )
    )
    ptu_resources = {d.resource_id.lower() for d in deps.values()}
    cost_by_resource: dict[str, Decimal] = defaultdict(Decimal)
    currencies: set[str] = set()
    reservations: list[ReservationPurchase] = []
    for line in lines:
        if looks_like_reservation(line.sku_meter, line.service_name):
            reservations.append(
                ReservationPurchase(
                    day=line.charge_period_start,
                    account_id=line.sub_account_id,
                    resource_id=line.resource_id,
                    sku_meter=line.sku_meter,
                    service_name=line.service_name,
                    cost=_dec(line.billed_cost).quantize(_MONEY_PLACES),
                    currency=line.billing_currency,
                )
            )
            currencies.add(line.billing_currency)
        elif (line.resource_id or "").lower() in ptu_resources and _is_ptu_charge(line):
            cost_by_resource[line.resource_id.lower()] += _dec(line.billed_cost)
            currencies.add(line.billing_currency)
    # v1 subscriptions bill in one currency; anything mixed falls back to the default.
    currency = currencies.pop() if len(currencies) == 1 else DEFAULT_CURRENCY

    shares = _shares(deps.values())
    out = []
    for key, d in sorted(deps.items(), key=lambda kv: deployment_key(*kv[0])):
        cost = (cost_by_resource.get(d.resource_id.lower(), Decimal(0)) * shares[key]).quantize(
            _MONEY_PLACES
        )
        avg = peak = idle_cost = None
        if d.values:
            avg = (sum(d.values) / len(d.values)).quantize(_UTIL_PLACES)
            peak = max(d.values)
            idle_cost = (cost * (1 - avg)).quantize(_MONEY_PLACES)
        out.append(
            PtuDeploymentReport(
                resource_id=d.resource_id,
                account_id=d.account_id,
                deployment=d.deployment,
                model=d.model,
                sku=d.sku,
                capacity=d.capacity,
                avg_utilization=avg,
                peak_utilization=peak,
                idle_hours=sum(1 for v in d.values if v < IDLE_THRESHOLD),
                hours=len(d.values),
                ptu_cost=cost,
                idle_cost=idle_cost,
                currency=currency,
            )
        )
    reservations.sort(key=lambda r: (r.day, r.account_id, r.sku_meter))
    return PtuReport(currency=currency, deployments=out, reservations=reservations)


async def ptu_hourly(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    resource_id: str,
    deployment: str,
    start: date,
    end: date,
) -> list[tuple[datetime, Decimal]]:
    """Stored hourly utilisation of one deployment, oldest first."""
    lo, hi = _day_bounds(start, end)
    rows = await session.execute(
        select(PtuUtilization.bucket_start, PtuUtilization.utilization)
        .where(
            PtuUtilization.tenant_id == tenant_id,
            PtuUtilization.resource_id == resource_id,
            PtuUtilization.deployment == deployment,
            PtuUtilization.bucket_start >= lo,
            PtuUtilization.bucket_start < hi,
        )
        .order_by(PtuUtilization.bucket_start)
    )
    return [(_hour(b), _dec(v).quantize(_UTIL_PLACES)) for b, v in rows]
