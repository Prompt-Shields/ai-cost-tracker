"""Allocation of AI spend to cost centres, teams and apps (issues #33, #34; ARCHITECTURE.md §4.4).

Billed cost already reaches deployments through ``api.queries`` (billed where billed, estimated
otherwise, resource cost split across same-model deployments). This module takes those
per-deployment amounts and maps each deployment to a cost centre / team / app:

- **Allocation rules**, evaluated by ascending priority then name; the first match wins.
  ``tag`` matches a deployment tag (key case-insensitive, value exact), ``name_pattern`` a
  case-insensitive glob on ``<account resource name>/<deployment>``, ``account`` the
  subscription id.
- **Split rules** override rules for one shared deployment: its cost is divided by fixed
  percentages, each part carrying its own cost centre / team / app, flagged approximate.
- Nothing matches: the cost is "unallocated", the untagged bucket that should shrink over time.
  Resource cost no deployment serves ("unattributed") is always unallocated and approximate.

Every piece is rounded to the money quantum with largest remainder, so the pieces of one
amount add up to that amount exactly and totals reconcile to ``/costs/daily``.
"""

import fnmatch
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_cost_tracker.api import queries as q
from ai_cost_tracker.config import Settings
from ai_cost_tracker.db.models import (
    AlertEvent,
    AllocationRule,
    DeploymentRow,
    SplitRule,
    Tenant,
)

UNALLOCATED = "unallocated"
MATCH_TYPES = ("tag", "name_pattern", "account")
LEVELS = ("cost_centre", "team", "app")
Level = Literal["cost_centre", "team", "app"]
# Tags every deployment should carry (ARCHITECTURE.md §4.4, item 2).
REQUIRED_TAGS = ["team", "app", "env", "cost-centre"]
UNTAGGED_ALERT_KIND = "untagged_weekly"
QUANTUM = Decimal("0.0000000001")  # Numeric(20, 10) in db/models.py
_ZERO = Decimal(0)


@dataclass(frozen=True)
class Allocation:
    """One destination of a deployment's cost; ``share`` is a fraction in 0..1."""

    cost_centre: str | None
    team: str | None
    app: str | None
    share: Decimal

    def key(self, level: str) -> str:
        return getattr(self, level) or UNALLOCATED


UNALLOCATED_ALL = Allocation(None, None, None, Decimal(1))


def resource_name(resource_id: str) -> str:
    """Last segment of an ARM id: the Cognitive Services account name."""
    return resource_id.rstrip("/").rsplit("/", 1)[-1] or resource_id


def _matches(rule: AllocationRule, deployment: DeploymentRow, account_resource_name: str) -> bool:
    if rule.match_type == "tag":
        wanted = (rule.match_key or "").lower()
        for k, v in (deployment.tags or {}).items():
            if str(k).lower() == wanted and str(v) == rule.match_value:
                return True
        return False
    if rule.match_type == "name_pattern":
        label = f"{account_resource_name}/{deployment.name}".lower()
        return fnmatch.fnmatchcase(label, rule.match_value.lower())
    if rule.match_type == "account":
        return deployment.account_id == rule.match_value
    return False


def _sorted_rules(rules: Sequence[AllocationRule]) -> list[AllocationRule]:
    return sorted(rules, key=lambda r: (r.priority, r.name))


def split_allocations(split: SplitRule) -> list[Allocation]:
    return [
        Allocation(
            a.get("cost_centre") or None,
            a.get("team") or None,
            a.get("app") or None,
            Decimal(str(a.get("percent", 0))) / 100,
        )
        for a in split.allocations or []
    ]


def resolve(
    deployment: DeploymentRow,
    account_resource_name: str,
    rules: list[AllocationRule],
    split: SplitRule | None,
) -> list[Allocation]:
    """Where a deployment's cost goes. A split wins over rules; no match -> unallocated."""
    if split is not None and split.active:
        parts = split_allocations(split)
        if parts:
            return parts
    for rule in _sorted_rules(rules):
        if rule.active and _matches(rule, deployment, account_resource_name):
            return [Allocation(rule.cost_centre, rule.team, rule.app, Decimal(1))]
    return [UNALLOCATED_ALL]


async def load_rules(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[list[AllocationRule], dict[tuple[str, str], SplitRule]]:
    """Active rules in evaluation order, and active splits keyed by (resource_id, deployment)."""
    rules = (
        await session.scalars(
            select(AllocationRule)
            .where(AllocationRule.tenant_id == tenant_id, AllocationRule.active.is_(True))
            .order_by(AllocationRule.priority, AllocationRule.name)
        )
    ).all()
    splits = (
        await session.scalars(
            select(SplitRule).where(SplitRule.tenant_id == tenant_id, SplitRule.active.is_(True))
        )
    ).all()
    return list(rules), {(s.resource_id, s.deployment): s for s in splits}


def distribute(amount: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``amount`` by ``weights`` into pieces that sum to ``amount`` exactly.

    Pieces are floored to QUANTUM; leftover quanta go to the largest remainders. Any residue
    below one quantum (an amount carrying more than 10 decimals) goes to the first of those.
    """
    total = sum(weights, _ZERO)
    if not weights or total <= 0:
        raise ValueError("weights must be positive")
    if len(weights) == 1:
        return [amount]
    exact = [amount * w / total for w in weights]
    pieces = [e.quantize(QUANTUM, rounding=ROUND_FLOOR) for e in exact]
    order = sorted(range(len(pieces)), key=lambda i: (-(exact[i] - pieces[i]), i))
    leftover = amount - sum(pieces, _ZERO)
    units = int((leftover / QUANTUM).to_integral_value(rounding=ROUND_FLOOR))
    for n in range(units):
        pieces[order[n % len(order)]] += QUANTUM
    pieces[order[0]] += amount - sum(pieces, _ZERO)
    return pieces


# --- cost-centre daily aggregation -------------------------------------------------------


@dataclass(frozen=True)
class CostCentreRow:
    day: date
    key: str
    cost: Decimal
    approximate: bool


def _deployment_for(inventory: q.Inventory, key: q.CellKey) -> DeploymentRow:
    row = inventory.by_key.get((key.resource_id, key.deployment))
    if row is not None:
        return row
    # Usage/estimates for a deployment the inventory never saw: match on what we know.
    return DeploymentRow(
        account_id=key.account_id,
        resource_id=key.resource_id,
        name=key.deployment or q.UNATTRIBUTED,
        model=key.model,
        tags=inventory.tags(key.resource_id, key.deployment),
    )


async def cost_centres_daily(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    filters: q.Filters | None = None,
    level: str = "cost_centre",
) -> list[CostCentreRow]:
    """Cost per day and cost centre / team / app; per-day totals equal ``/costs/daily``."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    filters = filters or q.Filters()
    inventory = await q.load_inventory(session, tenant_id)
    data = await q.load(session, tenant_id, start, end, filters, inventory)
    rules, splits = await load_rules(session, tenant_id)
    out: dict[tuple[date, str], list[Any]] = defaultdict(lambda: [_ZERO, False])
    for key, cell in data.cells.items():
        if not cell.has_cost:
            continue
        amount = data.effective(key, cell)[0]
        if key.deployment is None:
            bucket = out[(key.day, UNALLOCATED)]
            bucket[0] += amount
            bucket[1] = True
            continue
        shared = len(inventory.candidates(key.resource_id, key.model)) > 1
        split = splits.get((key.resource_id, key.deployment))
        dep = _deployment_for(inventory, key)
        allocs = resolve(dep, resource_name(key.resource_id), rules, split)
        approximate = shared or split is not None
        pieces = distribute(amount, [a.share for a in allocs])
        for alloc, piece in zip(allocs, pieces, strict=True):
            bucket = out[(key.day, alloc.key(level))]
            bucket[0] += piece
            bucket[1] = bucket[1] or approximate
    return [CostCentreRow(d, k, v[0], v[1]) for (d, k), v in sorted(out.items())]


# --- untagged report ---------------------------------------------------------------------


@dataclass(frozen=True)
class UntaggedDeployment:
    id: str
    name: str
    account_id: str
    resource_id: str
    account_name: str
    model: str
    cost: Decimal
    missing_tags: list[str]


@dataclass(frozen=True)
class UntaggedReport:
    total: Decimal
    unattributed_cost: Decimal
    deployments: list[UntaggedDeployment]


def missing_tags(tags: dict[str, Any] | None) -> list[str]:
    """Required tags absent or blank on a deployment (keys compared case-insensitively)."""
    present = {str(k).lower() for k, v in (tags or {}).items() if str(v or "").strip()}
    return [t for t in REQUIRED_TAGS if t not in present]


async def untagged_report(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date
) -> UntaggedReport:
    """Deployments missing a required tag, with their cost over [start, end].

    Live deployments are listed even at zero cost (owners should tag them before they spend);
    deleted ones only when they cost something in range. ``unattributed_cost`` is resource
    cost no deployment serves: untagged too, but nobody to ask.
    """
    inventory = await q.load_inventory(session, tenant_id)
    data = await q.load(session, tenant_id, start, end, q.Filters(), inventory)
    cost: dict[tuple[str, str], Decimal] = defaultdict(lambda: _ZERO)
    unattributed = _ZERO
    for key, cell in data.cells.items():
        amount = data.effective(key, cell)[0]
        if key.deployment is None:
            unattributed += amount
        else:
            cost[(key.resource_id, key.deployment)] += amount
    items = []
    for row in inventory.rows:
        missing = missing_tags(row.tags)
        spent = cost.get((row.resource_id, row.name), _ZERO)
        if not missing or (row.deleted_at is not None and not spent):
            continue
        items.append(
            UntaggedDeployment(
                id=str(row.id),
                name=row.name,
                account_id=row.account_id,
                resource_id=row.resource_id,
                account_name=resource_name(row.resource_id),
                model=row.model,
                cost=spent,
                missing_tags=missing,
            )
        )
    items.sort(key=lambda d: (-d.cost, d.account_name, d.name))
    return UntaggedReport(sum((d.cost for d in items), _ZERO), unattributed, items)


# --- weekly job --------------------------------------------------------------------------


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


async def run_untagged_weekly(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    """On Mondays (UTC), raise one info alert listing last week's untagged deployments.

    Keyed by the reported ISO week, so re-runs within the week refresh the same row; it is
    re-sent only when the list grows. Returns the number of untagged deployments reported.
    """
    now = now or datetime.now(UTC)
    today = now.astimezone(UTC).date()
    if today.weekday() != 0:
        return 0
    start, end = today - timedelta(days=7), today - timedelta(days=1)
    subject = iso_week(start)
    async with session_factory() as session:
        tenant_id = await session.scalar(
            select(Tenant.id).where(Tenant.slug == settings.default_tenant_slug)
        )
        if tenant_id is None:
            return 0
        report = await untagged_report(session, tenant_id, start, end)
        alert = await session.scalar(
            select(AlertEvent).where(
                AlertEvent.tenant_id == tenant_id,
                AlertEvent.kind == UNTAGGED_ALERT_KIND,
                AlertEvent.subject == subject,
            )
        )
        if not report.deployments:
            if alert is not None and alert.resolved_at is None:
                alert.resolved_at = now
                await session.commit()
            return 0
        currency = await q.currency(session, tenant_id, start, end)
        ids = [d.id for d in report.deployments]
        details = {
            "week": subject,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "currency": currency,
            "total_cost": _money(report.total),
            "unattributed_cost": _money(report.unattributed_cost),
            "required_tags": REQUIRED_TAGS,
            "deployments": [
                {
                    "id": d.id,
                    "deployment": f"{d.account_name}/{d.name}",
                    "account_id": d.account_id,
                    "model": d.model,
                    "cost": _money(d.cost),
                    "missing_tags": d.missing_tags,
                }
                for d in report.deployments
            ],
        }
        message = (
            f"{len(ids)} deployment(s) missing required tags cost {_money(report.total, 2)} "
            f"{currency} in {subject}"
        )
        if alert is None:
            alert = AlertEvent(tenant_id=tenant_id, kind=UNTAGGED_ALERT_KIND, subject=subject)
            session.add(alert)
        else:
            before = {d.get("id") for d in (alert.details or {}).get("deployments", [])}
            if not set(ids) <= before:
                alert.notified_at = None
        alert.severity = "info"
        alert.message = message
        alert.details = details
        alert.resolved_at = None
        await session.commit()
        return len(ids)


def _money(value: Decimal, places: int = 10) -> str:
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")
