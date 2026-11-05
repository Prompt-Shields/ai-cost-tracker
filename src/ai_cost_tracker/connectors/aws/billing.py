"""CUR 2.0 Data Export (S3, CSV + GZIP) -> daily ``CostRecord`` per account, region and usage type.

The Data Export is the AWS source whose numbers match Cost Explorer, so billed cost comes
from there rather than from the price list x tokens (ARCHITECTURE.md §8). We read the
export's files straight from S3 through ``AwsSessions`` (no Athena): one billing period at a
time, streamed through gzip and ``csv`` so a large month never sits in memory.

Layout and columns follow AWS's public docs (BCM Data Exports user guide, "CUR 2.0 table
dictionary"). Not yet checked on a live export:

- Assumption (spike-aws): files land under
  ``<prefix>/<name>/data/BILLING_PERIOD=YYYY-MM/`` with a manifest
  ``<prefix>/<name>/metadata/BILLING_PERIOD=YYYY-MM/<name>-Manifest.json`` whose
  ``dataFiles`` list holds ``s3://bucket/key`` URIs of the current files. Manifest first;
  without one we list the data prefix and, if it holds several execution folders
  (create-new-report mode), use only the most recently written one so nothing is counted
  twice.
- Assumption (spike-aws): the CSV header row carries the CUR 2.0 snake_case names in
  ``CUR_COLUMNS``; we look columns up by name, so order and extra columns don't matter.
- Assumption (spike-aws): first-party Bedrock rows have ``line_item_product_code =
  'AmazonBedrock'``. Third-party models sold through AWS Marketplace ("Claude ... (Amazon
  Bedrock Edition)") carry a Marketplace product code instead; we recognise them by
  ``bill_billing_entity = 'AWS Marketplace'`` plus "Amazon Bedrock Edition" in
  ``line_item_line_item_description``.
- Assumption (spike-aws): ``line_item_usage_start_date`` is UTC ISO 8601
  (``2026-10-01T00:00:00Z``); usage types start with a region prefix such as ``USE1-``.
- Assumption (spike-aws): credit and refund rows carry the service's product code and usage
  account; a credit with no usage type is kept under its line item type (``Credit``) as
  ``sku_meter`` so the day's net total still matches.

Decisions (contract "AWS model"):
- Day = UTC date of ``line_item_usage_start_date``; rows outside [start, end] are dropped,
  whatever billing period file they came from.
- ``billed_cost`` = sum of ``line_item_unblended_cost`` over every line item type except
  ``Tax`` (credits, refunds and fees included): net of credits, like GCP.
- ``sku_meter`` = usage type without the region prefix (the price list's key too).
- Zero-cost rows are kept: fully credited usage is a real billed 0.
- Money is parsed from the CSV string straight into ``Decimal``, never via float.
"""

import asyncio
import csv
import gzip
import io
import json
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ai_cost_tracker.connectors.aws.ids import ExportLocation, resource_id
from ai_cost_tracker.core.models import CostRecord

log = logging.getLogger(__name__)

# Columns we read, by CUR 2.0 name. W6's CloudFormation export selects exactly these.
CUR_COLUMNS: tuple[str, ...] = (
    "bill_billing_entity",
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_usage_account_id",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_line_item_description",
    "line_item_usage_amount",
    "line_item_unblended_cost",
    "line_item_currency_code",
    "product_region_code",
)
# Columns the parser can't do without; the rest only refine the result.
REQUIRED_COLUMNS = (
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_usage_account_id",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_unblended_cost",
)

BEDROCK_PRODUCT_CODES = frozenset({"AmazonBedrock"})
BEDROCK_SERVICE_NAME = "Amazon Bedrock"
MARKETPLACE_ENTITY = "aws marketplace"
MARKETPLACE_MARKER = "amazon bedrock edition"
EXCLUDED_LINE_ITEM_TYPES = frozenset({"tax"})

# USE1-, USW2-, EUC1-, APN1-, UGW1- ... Assumption (spike-aws): AWS's region prefixes.
_REGION_PREFIX = re.compile(r"^[A-Z]{2,4}\d-")
_MARKETPLACE_NAME = re.compile(r"^(.*?\(Amazon Bedrock Edition\))", re.IGNORECASE)


class CurFormatError(RuntimeError):
    """An export file isn't the CSV the export definition promised (missing columns, Parquet)."""


@dataclass
class _Sum:
    cost: Decimal
    quantity: Decimal


# (day, region, sku_meter, service_name, currency) -> running sums
_Key = tuple[date, str, str, str, str]


async def pull_cost(
    sessions: Any, export: ExportLocation, account_id: str, start: date, end: date
) -> list[CostRecord]:
    """Billed Bedrock cost per UTC day, region and usage type for one account.

    ``start`` and ``end`` are inclusive UTC days. A billing period with no files yet (new
    export, first delivery pending) contributes nothing rather than failing the pull.
    """
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    client = await sessions.client("s3", export.account_id, export.region)
    totals: dict[_Key, _Sum] = {}
    for period in billing_periods(start, end):
        files = await _period_files(sessions, client, export, period)
        if not files:
            log.info("aws billing: no export files for %s yet", period)
            continue
        for bucket, key in files:
            obj = await sessions.call(client, "get_object", Bucket=bucket, Key=key)
            part = await asyncio.to_thread(
                _parse_object, obj.get("Body"), key, account_id, start, end
            )
            _merge(totals, part)
    return [_record(account_id, k, v) for k, v in sorted(totals.items())]


def billing_periods(start: date, end: date) -> list[str]:
    """``YYYY-MM`` of every calendar month touching [start, end] (a CUR billing period)."""
    out: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def strip_region(usage_type: str) -> str:
    """``USE1-Claude3.7Sonnet-input-tokens`` -> ``Claude3.7Sonnet-input-tokens``."""
    return _REGION_PREFIX.sub("", usage_type.strip(), count=1)


def _base(export: ExportLocation) -> str:
    return "/".join(p.strip("/") for p in (export.prefix, export.name) if p and p.strip("/"))


async def _period_files(
    sessions: Any, client: Any, export: ExportLocation, period: str
) -> list[tuple[str, str]]:
    """(bucket, key) of the period's current data files: manifest first, else a listing."""
    base = _base(export)
    meta_prefix = f"{base}/metadata/BILLING_PERIOD={period}/"
    metas = await sessions.paginate(
        client, "list_objects_v2", "Contents", Bucket=export.bucket, Prefix=meta_prefix
    )
    manifests = [o for o in metas if o.get("Key", "").lower().endswith("manifest.json")]
    if manifests:
        latest = max(manifests, key=lambda o: (_modified(o), o["Key"]))
        obj = await sessions.call(client, "get_object", Bucket=export.bucket, Key=latest["Key"])
        files = manifest_files(_read_all(obj.get("Body")), export.bucket)
        if files:
            return files
        log.warning("aws billing: manifest %s lists no data files; listing", latest["Key"])

    data_prefix = f"{base}/data/BILLING_PERIOD={period}/"
    objects = await sessions.paginate(
        client, "list_objects_v2", "Contents", Bucket=export.bucket, Prefix=data_prefix
    )
    return [(export.bucket, k) for k in latest_execution(objects, data_prefix)]


def manifest_files(raw: bytes, default_bucket: str) -> list[tuple[str, str]]:
    """(bucket, key) pairs from a Data Exports manifest's ``dataFiles``.

    Assumption (spike-aws): entries are ``s3://bucket/key`` strings; bare keys and
    ``{"key": ...}`` objects are accepted too in case the shape differs.
    """
    doc = json.loads(raw.decode("utf-8-sig"))
    out: list[tuple[str, str]] = []
    for entry in doc.get("dataFiles") or []:
        if isinstance(entry, dict):
            entry = entry.get("s3Uri") or entry.get("key") or entry.get("Key") or ""
        if not isinstance(entry, str) or not entry:
            continue
        if entry.startswith("s3://"):
            bucket, _, key = entry[len("s3://") :].partition("/")
            out.append((bucket or default_bucket, key))
        else:
            out.append((default_bucket, entry.lstrip("/")))
    return out


def latest_execution(objects: Iterable[dict[str, Any]], data_prefix: str) -> list[str]:
    """Data file keys under ``data_prefix``, from the newest execution folder only.

    Overwrite mode keeps files directly under the period; create-new mode adds a folder per
    execution, and summing every folder would multiply the month.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for o in objects:
        key = o.get("Key", "")
        rest = key[len(data_prefix) :] if key.startswith(data_prefix) else key
        if not _is_data_file(key):
            continue
        folder = rest.rpartition("/")[0]
        groups.setdefault(folder, []).append(o)
    if not groups:
        return []
    newest = max(groups.values(), key=lambda g: max(_modified(o) for o in g))
    return sorted(o["Key"] for o in newest)


def _is_data_file(key: str) -> bool:
    k = key.lower()
    return k.endswith((".csv.gz", ".csv", ".parquet", ".snappy.parquet"))


def _modified(obj: dict[str, Any]) -> datetime:
    value = obj.get("LastModified")
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        return _parse_ts(value)
    return datetime.min.replace(tzinfo=UTC)


def _read_all(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    try:
        return body.read()
    finally:
        close = getattr(body, "close", None)
        if close:
            close()


def _parse_object(body: Any, key: str, account_id: str, start: date, end: date) -> dict[_Key, _Sum]:
    """One export file's matching rows, aggregated. Runs in a thread: S3 reads block."""
    if key.lower().endswith(".parquet"):
        raise CurFormatError(f"{key}: Parquet export; the export must be CSV with GZIP")
    stream: Any = io.BytesIO(bytes(body)) if isinstance(body, bytes | bytearray) else body
    if stream is None:
        return {}
    try:
        raw = gzip.GzipFile(fileobj=stream, mode="rb") if key.lower().endswith(".gz") else stream
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        totals: dict[_Key, _Sum] = {}
        for k, cost, qty in _rows(csv.reader(text), key, account_id, start, end):
            s = totals.get(k)
            if s is None:
                totals[k] = _Sum(cost, qty)
            else:
                s.cost += cost
                s.quantity += qty
        return totals
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


def _rows(
    reader: Iterator[list[str]], key: str, account_id: str, start: date, end: date
) -> Iterator[tuple[_Key, Decimal, Decimal]]:
    """Filtered rows as (aggregation key, cost, quantity); columns found by header name."""
    header = next(reader, None)
    if header is None:
        return
    idx = {name.strip().lower(): i for i, name in enumerate(header)}
    missing = [c for c in REQUIRED_COLUMNS if c not in idx]
    if missing:
        raise CurFormatError(f"{key}: export is missing columns {', '.join(missing)}")

    def col(name: str):
        i = idx.get(name)
        return (lambda row: row[i] if i < len(row) else "") if i is not None else (lambda row: "")

    line_type, usage_start = col("line_item_line_item_type"), col("line_item_usage_start_date")
    usage_account, product_code = col("line_item_usage_account_id"), col("line_item_product_code")
    usage_type, description = col("line_item_usage_type"), col("line_item_line_item_description")
    amount, cost = col("line_item_usage_amount"), col("line_item_unblended_cost")
    currency, region = col("line_item_currency_code"), col("product_region_code")
    entity = col("bill_billing_entity")

    for n, row in enumerate(reader, start=2):
        if not row or usage_account(row).strip() != account_id:
            continue
        ltype = line_type(row).strip()
        if ltype.lower() in EXCLUDED_LINE_ITEM_TYPES:
            continue
        service = _bedrock_service(product_code(row), entity(row), description(row))
        if service is None:
            continue
        day = _parse_ts(usage_start(row)).date()
        if day < start or day > end:
            continue
        sku = strip_region(usage_type(row)) or ltype or "unknown"
        k = (day, region(row).strip().lower(), sku, service, currency(row).strip() or "USD")
        yield k, _dec(cost(row), key, n), _dec(amount(row), key, n)


def _bedrock_service(product_code: str, entity: str, description: str) -> str | None:
    """Service name for a Bedrock row (first-party or Marketplace edition), else None."""
    if product_code.strip() in BEDROCK_PRODUCT_CODES:
        return BEDROCK_SERVICE_NAME
    if entity.strip().lower() == MARKETPLACE_ENTITY and MARKETPLACE_MARKER in description.lower():
        m = _MARKETPLACE_NAME.match(description.strip())
        return m.group(1).strip() if m else "AWS Marketplace (Amazon Bedrock Edition)"
    return None


def _parse_ts(value: str) -> datetime:
    """UTC datetime from CUR's ISO 8601 timestamp (``Z``, offset or naive = UTC)."""
    v = value.strip()
    if not v:
        raise CurFormatError("row without line_item_usage_start_date")
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v.replace(" ", "T", 1))
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dec(value: str, key: str, line: int) -> Decimal:
    v = value.strip()
    if not v:
        return Decimal(0)
    try:
        return Decimal(v)
    except InvalidOperation as exc:
        raise CurFormatError(f"{key}:{line}: not a number: {v!r}") from exc


def _merge(totals: dict[_Key, _Sum], part: dict[_Key, _Sum]) -> None:
    for k, v in part.items():
        s = totals.get(k)
        if s is None:
            totals[k] = v
        else:
            s.cost += v.cost
            s.quantity += v.quantity


def _record(account_id: str, key: _Key, s: _Sum) -> CostRecord:
    day, region, sku, service, currency = key
    return CostRecord(
        cloud="aws",
        sub_account_id=account_id,
        resource_id=resource_id(account_id, region),
        charge_period_start=day,
        service_name=service,
        sku_meter=sku,
        consumed_quantity=s.quantity,
        billed_cost=s.cost,
        billing_currency=currency,
    )
