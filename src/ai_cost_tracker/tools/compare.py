"""``act compare-cost-analysis``: our daily cost vs a Cost Analysis CSV export (issue #27).

The M1 exit criterion is "yesterday's AI cost per deployment within 1% of Cost Analysis".
Cost Analysis is what finance looks at, so it is the reference: each day's difference is
measured against its figure. Exports differ by offer and portal version (``Cost`` /
``PreTaxCost`` / ``CostUSD`` headers, ``yyyymmdd`` / ISO / ``MM/DD/YYYY`` dates, ``;`` with
decimal commas in some locales), so the reader finds columns by name and sniffs formats.
"""

import csv
import io
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_cost_tracker.connectors.azure.cost_query import AI_RESOURCE_TYPES
from ai_cost_tracker.db.models import CostLine, Tenant

# Header candidates, compared after lower-casing and dropping spaces / underscores.
# Cost order: billing-currency columns before USD ones, because CostLine holds billing currency.
DATE_HEADERS = ("usagedate", "date", "usagedatetime", "day", "chargeperiodstart")
COST_HEADERS = (
    "cost",
    "pretaxcost",
    "costinbillingcurrency",
    "billedcost",
    "costusd",
    "pretaxcostusd",
    "costinusd",
)
RESOURCE_HEADERS = ("resourceid", "instanceid")
CURRENCY_HEADERS = ("currency", "billingcurrency", "billingcurrencycode")

DEFAULT_TOLERANCE_PCT = Decimal("1")
# Differences below a cent are rounding, not a mismatch, however large in percent.
DEFAULT_MIN_ABS = Decimal("0.01")
_HEADER_SCAN_ROWS = 20
_TEXT_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y")
_SLASH_DATE = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?:[ T].*)?")


@dataclass
class ExportRow:
    day: date
    resource_id: str  # lower-cased; "" when the export isn't grouped by resource
    cost: Decimal


@dataclass
class Export:
    rows: list[ExportRow]
    cost_column: str
    currencies: set[str] = field(default_factory=set)
    skipped_rows: int = 0  # rows for resource types we don't track
    warnings: list[str] = field(default_factory=list)

    @property
    def first_day(self) -> date | None:
        return min((r.day for r in self.rows), default=None)

    @property
    def last_day(self) -> date | None:
        return max((r.day for r in self.rows), default=None)


def _norm(header: str) -> str:
    return re.sub(r"[\s_]", "", header.strip().lstrip("﻿")).lower()


def _find(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    normed = [_norm(h) for h in headers]
    return next((normed.index(c) for c in candidates if c in normed), None)


def parse_amount(text: str, decimal_comma: bool = False) -> Decimal:
    value = re.sub(r"[\s$€£]", "", text or "")
    if not value:
        return Decimal(0)
    if decimal_comma:
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", "")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc


def detect_dayfirst(values: list[str]) -> tuple[bool, bool]:
    """(dayfirst, ambiguous) for ``NN/NN/YYYY`` dates; US month-first when undecidable."""
    for value in values:
        if m := _SLASH_DATE.fullmatch(value.strip()):
            if int(m[1]) > 12:
                return True, False
            if int(m[2]) > 12:
                return False, False
    has_slash = any(_SLASH_DATE.fullmatch(v.strip()) for v in values)
    return False, has_slash


def parse_day(text: str, dayfirst: bool = False) -> date:
    value = text.strip()
    if re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    if re.match(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value[:10])
    if m := re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:[ T].*)?", value):
        return date(int(m[1]), int(m[2]), int(m[3]))
    if m := _SLASH_DATE.fullmatch(value):
        a, b, year = int(m[1]), int(m[2]), int(m[3])
        return date(year, b, a) if dayfirst else date(year, a, b)
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {text!r}")


def _is_tracked(resource_id: str) -> bool:
    return any(f"/providers/{t}/" in resource_id for t in AI_RESOURCE_TYPES)


def read_export(path: str | Path, **kwargs: Any) -> Export:
    """Read a Cost Analysis CSV file (a UTF-8 BOM, as the portal writes, is fine)."""
    return parse_export(Path(path).read_text(encoding="utf-8-sig"), **kwargs)


def parse_export(
    text: str,
    *,
    cost_column: str | None = None,
    dayfirst: bool | None = None,
    subscription_ids: list[str] | None = None,
    all_resources: bool = False,
) -> Export:
    """Parse Cost Analysis CSV text. Rows for untracked resource types are skipped unless
    ``all_resources``, since our database only holds AI resource types.
    """
    text = text.lstrip("\ufeff")
    cost_candidates = (_norm(cost_column),) if cost_column else COST_HEADERS
    # Try each delimiter and keep the first that yields a recognisable header row; sniffing
    # is unreliable when the portal adds a preamble above the table.
    for delimiter in (",", ";", "\t"):
        table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        header_i = next(
            (
                i
                for i, row in enumerate(table[:_HEADER_SCAN_ROWS])
                if _find(row, DATE_HEADERS) is not None and _find(row, cost_candidates) is not None
            ),
            None,
        )
        if header_i is not None:
            break
    else:
        raise ValueError(
            "no header row with a date column (UsageDate/Date) and a cost column "
            "(Cost/PreTaxCost/CostUSD/...) in the first rows"
        )
    headers = table[header_i]
    date_i = _find(headers, DATE_HEADERS)
    cost_i = _find(headers, cost_candidates)
    res_i = _find(headers, RESOURCE_HEADERS)
    cur_i = _find(headers, CURRENCY_HEADERS)
    assert date_i is not None and cost_i is not None
    body = [r for r in table[header_i + 1 :] if len(r) > max(date_i, cost_i) and r[date_i].strip()]

    export = Export(rows=[], cost_column=headers[cost_i].strip().lstrip("﻿"))
    if dayfirst is None:
        dayfirst, ambiguous = detect_dayfirst([r[date_i] for r in body])
        if ambiguous:
            export.warnings.append("dates like 03/04/2026 read as month/day; use --dayfirst if not")
    subs = [f"/subscriptions/{s.lower()}/" for s in subscription_ids or []]
    if res_i is None:
        export.warnings.append("export has no ResourceId column: per-resource comparison skipped")
    for row in body:
        resource = row[res_i].strip().lower().rstrip("/") if res_i is not None else ""
        if resource and subs and not any(resource.startswith(s) for s in subs):
            export.skipped_rows += 1
            continue
        if resource and not all_resources and not _is_tracked(resource + "/"):
            export.skipped_rows += 1
            continue
        export.rows.append(
            ExportRow(
                day=parse_day(row[date_i], dayfirst),
                resource_id=resource,
                cost=parse_amount(row[cost_i], decimal_comma=delimiter == ";"),
            )
        )
        if cur_i is not None and row[cur_i].strip():
            export.currencies.add(row[cur_i].strip().upper())
    return export


@dataclass
class Totals:
    by_day: dict[date, Decimal] = field(default_factory=dict)
    by_resource: dict[str, Decimal] = field(default_factory=dict)
    currencies: set[str] = field(default_factory=set)


def export_totals(export: Export, start: date, end: date) -> Totals:
    totals = Totals(defaultdict(Decimal), defaultdict(Decimal), set(export.currencies))
    for row in export.rows:
        if start <= row.day <= end:
            totals.by_day[row.day] += row.cost
            if row.resource_id:
                totals.by_resource[row.resource_id] += row.cost
    return totals


async def tenant_id_for(session: AsyncSession, slug: str) -> uuid.UUID:
    tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tenant_id is None:
        raise ValueError(f"no tenant {slug!r} in the database; run an ingest first")
    return tenant_id


async def load_ours(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    start: date,
    end: date,
    subscription_ids: list[str] | None = None,
) -> Totals:
    """Billed Azure cost from ``CostLine`` per day and per resource, for one tenant."""
    where = [
        CostLine.tenant_id == tenant_id,
        CostLine.cloud == "azure",
        CostLine.charge_period_start >= start,
        CostLine.charge_period_start <= end,
    ]
    if subscription_ids:
        where.append(func.lower(CostLine.sub_account_id).in_([s.lower() for s in subscription_ids]))
    totals = Totals()
    day_rows = await session.execute(
        select(CostLine.charge_period_start, func.sum(CostLine.billed_cost))
        .where(*where)
        .group_by(CostLine.charge_period_start)
    )
    totals.by_day = {d: Decimal(str(v or 0)) for d, v in day_rows}
    res_rows = await session.execute(
        select(func.lower(CostLine.resource_id), func.sum(CostLine.billed_cost))
        .where(*where)
        .group_by(func.lower(CostLine.resource_id))
    )
    totals.by_resource = {(r or "").rstrip("/"): Decimal(str(v or 0)) for r, v in res_rows}
    cur_rows = await session.scalars(select(CostLine.billing_currency).where(*where).distinct())
    totals.currencies = {c.upper() for c in cur_rows}
    return totals


@dataclass
class Diff:
    key: str
    theirs: Decimal
    ours: Decimal

    @property
    def diff(self) -> Decimal:
        return self.ours - self.theirs

    @property
    def pct(self) -> Decimal | None:
        """Difference in percent of the Cost Analysis figure; None means infinite."""
        if self.theirs == 0:
            return Decimal(0) if self.ours == 0 else None
        return abs(self.diff) / abs(self.theirs) * 100

    def over(self, tolerance_pct: Decimal, min_abs: Decimal) -> bool:
        if abs(self.diff) <= min_abs:
            return False
        pct = self.pct
        return pct is None or pct > tolerance_pct


@dataclass
class Comparison:
    start: date
    end: date
    tolerance_pct: Decimal
    min_abs: Decimal
    days: list[Diff]
    resources: list[Diff]
    warnings: list[str] = field(default_factory=list)

    @property
    def failed_days(self) -> list[Diff]:
        return [d for d in self.days if d.over(self.tolerance_pct, self.min_abs)]

    @property
    def passed(self) -> bool:
        return not self.failed_days


def compare(
    theirs: Totals,
    ours: Totals,
    start: date,
    end: date,
    tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT,
    min_abs: Decimal = DEFAULT_MIN_ABS,
) -> Comparison:
    """Every day in ``start``..``end`` and every resource on either side, side by side.

    A day missing on one side counts as zero there, so a day we never ingested fails.
    """
    days = []
    day = start
    while day <= end:
        t, o = theirs.by_day.get(day, Decimal(0)), ours.by_day.get(day, Decimal(0))
        if t or o:
            days.append(Diff(day.isoformat(), t, o))
        day += timedelta(days=1)
    resources = []
    if theirs.by_resource:
        for key in sorted(set(theirs.by_resource) | set(ours.by_resource)):
            t = theirs.by_resource.get(key, Decimal(0))
            o = ours.by_resource.get(key, Decimal(0))
            resources.append(Diff(key, t, o))
        resources.sort(key=lambda d: abs(d.diff), reverse=True)
    warnings = []
    if theirs.currencies and ours.currencies and theirs.currencies != ours.currencies:
        warnings.append(
            f"currency differs: export {sorted(theirs.currencies)}, "
            f"database {sorted(ours.currencies)} (use --cost-column to pick a matching column)"
        )
    return Comparison(start, end, tolerance_pct, min_abs, days, resources, warnings)


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001')):,}"


def _pct(d: Diff) -> str:
    pct = d.pct
    return "inf" if pct is None else f"{pct.quantize(Decimal('0.01'))}%"


def _short(resource_id: str) -> str:
    """``<resource group>/<name>`` is enough to recognise a resource in a terminal table."""
    parts = resource_id.split("/")
    try:
        rg = parts[parts.index("resourcegroups") + 1]
    except (ValueError, IndexError):
        return resource_id or "(no resource)"
    return f"{rg}/{parts[-1]}"


def render_table(c: Comparison, max_resources: int = 20) -> str:
    lines = [
        f"Cost Analysis vs ai-cost-tracker, {c.start} to {c.end} "
        f"(tolerance {c.tolerance_pct}% per day, ignoring differences up to {c.min_abs})",
        "",
        f"{'Day':<12}{'Cost Analysis':>16}{'Ours':>16}{'Diff':>14}{'Diff %':>10}  Status",
    ]
    for d in c.days:
        status = "OVER" if d.over(c.tolerance_pct, c.min_abs) else "ok"
        lines.append(
            f"{d.key:<12}{_money(d.theirs):>16}{_money(d.ours):>16}{_money(d.diff):>14}"
            f"{_pct(d):>10}  {status}"
        )
    if c.resources:
        lines += ["", f"Per resource over the range (largest {max_resources} differences):", ""]
        lines.append(f"{'Resource':<48}{'Cost Analysis':>16}{'Ours':>16}{'Diff':>14}{'Diff %':>10}")
        for d in c.resources[:max_resources]:
            lines.append(
                f"{_short(d.key)[:47]:<48}{_money(d.theirs):>16}{_money(d.ours):>16}"
                f"{_money(d.diff):>14}{_pct(d):>10}"
            )
    lines += [""] + [f"warning: {w}" for w in c.warnings]
    failed = len(c.failed_days)
    verdict = "PASS" if c.passed else "FAIL"
    lines.append(
        f"{verdict}: {failed} of {len(c.days)} days differ by more than {c.tolerance_pct}%"
    )
    return "\n".join(lines)


def to_json(c: Comparison) -> dict[str, Any]:
    def row(d: Diff, name: str) -> dict[str, Any]:
        pct = d.pct
        return {
            name: d.key,
            "cost_analysis": str(d.theirs),
            "ours": str(d.ours),
            "diff": str(d.diff),
            "diff_pct": None if pct is None else str(pct.quantize(Decimal("0.0001"))),
            "over": d.over(c.tolerance_pct, c.min_abs),
        }

    return {
        "start": c.start.isoformat(),
        "end": c.end.isoformat(),
        "tolerance_pct": str(c.tolerance_pct),
        "min_abs": str(c.min_abs),
        "passed": c.passed,
        "failed_days": len(c.failed_days),
        "days": [row(d, "day") for d in c.days],
        "resources": [row(d, "resource_id") for d in c.resources],
        "warnings": c.warnings,
    }


async def run_comparison(
    session: AsyncSession,
    tenant_slug: str,
    export: Export,
    start: date | None = None,
    end: date | None = None,
    subscription_ids: list[str] | None = None,
    tolerance_pct: Decimal = DEFAULT_TOLERANCE_PCT,
    min_abs: Decimal = DEFAULT_MIN_ABS,
) -> Comparison:
    """Compare the export with the database over ``start``..``end`` (default: the export's)."""
    start = start or export.first_day
    end = end or export.last_day
    if start is None or end is None:
        raise ValueError("the export has no rows for tracked AI resources")
    tenant_id = await tenant_id_for(session, tenant_slug)
    ours = await load_ours(session, tenant_id, start, end, subscription_ids)
    result = compare(export_totals(export, start, end), ours, start, end, tolerance_pct, min_abs)
    result.warnings = export.warnings + result.warnings
    if export.skipped_rows:
        result.warnings.append(f"skipped {export.skipped_rows} export rows for untracked resources")
    return result
