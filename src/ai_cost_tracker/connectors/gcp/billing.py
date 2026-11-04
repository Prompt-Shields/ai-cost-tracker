"""Cloud Billing BigQuery export -> daily ``CostRecord`` per project, location and SKU.

The detailed (resource-level) usage cost export is the only GCP source whose numbers match
the billing console, so cost comes from there rather than from the Catalog API x tokens
(ARCHITECTURE.md §8). We run one aggregate query through the BigQuery REST API
(``jobs.query`` then ``jobs.getQueryResults``) instead of the BigQuery SDK, which keeps the
connector on plain httpx behind ``GcpClient``.

Shapes follow Google's public docs (Cloud Billing "Structure of Detailed data export",
BigQuery REST ``jobs.query`` / ``jobs.getQueryResults``). Not yet checked on a live export:

- Assumption (spike-gcp): Vertex AI rows carry ``service.description = 'Vertex AI'``. Gemini
  Developer API ("Gemini API", AI Studio keys) and Marketplace partner models (Claude, Llama
  on Vertex, billed under their own service names) are not included: their tokens don't
  appear in the Vertex AI metrics we join with. Extend ``SERVICES`` once the spike lists them.
- Assumption (spike-gcp): ``location.region`` is set for regional Vertex AI usage and null
  for global endpoints, where ``location.location`` holds ``global`` (or a multi-region);
  we take ``COALESCE(region, location)`` and missing means ``global`` (``ids.resource_id``).
- Assumption (spike-gcp): ``cost``, ``credits.amount`` and ``usage.amount_in_pricing_units``
  are FLOAT64. We ``CAST`` them to NUMERIC in SQL (rounds to 9 dp) so BigQuery returns exact
  decimal strings and float noise never reaches the sums.
- Assumption (spike-gcp): the table is ingestion-time partitioned on ``_PARTITIONTIME`` and
  rows for a usage day land on or after that day (late rows and corrections can arrive
  days later). We prune with a lower bound only, ``start`` minus ``PARTITION_MARGIN_DAYS``
  (the export partitions on Pacific time), so a re-run picks up late rows; the scan grows
  with the age of ``start``, which backfill (≤ 90 days) keeps bounded.

Decisions (contract "GCP model"):
- Day = UTC date of ``usage_start_time``. The console defaults to Pacific time, so a day's
  total can differ from the console's view of that day; totals over a month still match.
- ``billed_cost = cost + SUM(credits.amount)`` (credits are negative); ``cost_type = 'tax'``
  rows are excluded; ``adjustment`` and ``rounding_error`` rows are kept, as the console's
  cost total includes them.
- Zero-cost rows are **kept**: free-tier or fully credited usage is a real billed 0, and
  dropping it would make the overview fall back to the token estimate for that day/SKU.
"""

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from ai_cost_tracker.connectors.gcp.ids import resource_id
from ai_cost_tracker.core.models import CostRecord

BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"

SERVICES = ("Vertex AI",)
PARTITION_MARGIN_DAYS = 2

# Long-poll per getQueryResults call, and how many calls before giving up (~10 minutes).
POLL_TIMEOUT_MS = 10_000
MAX_POLLS = 60

_TABLE_RE = re.compile(r"^[\w-]+\.[\w-]+\.[\w-]+$", re.ASCII)
_DECIMAL_TYPES = {"NUMERIC", "BIGNUMERIC", "FLOAT", "FLOAT64"}
_INT_TYPES = {"INTEGER", "INT64"}


class BillingQueryError(RuntimeError):
    """The billing export query failed or didn't finish in time."""


def validate_table(table: str) -> tuple[str, str, str]:
    """``project.dataset.table`` split in three; ValueError for anything else.

    The name ends up inside SQL (identifiers can't be query parameters), so only letters,
    digits, ``_`` and ``-`` are allowed: no backticks, spaces, dots or quotes can sneak in.
    """
    if not isinstance(table, str) or not _TABLE_RE.fullmatch(table):
        raise ValueError(
            f"invalid ACT_GCP_BILLING_EXPORT_TABLE {table!r}: expected <project>.<dataset>.<table>"
        )
    project, dataset, name = table.split(".")
    return project, dataset, name


def build_query(table: str, project_id: str, start: date, end: date) -> tuple[str, list[dict]]:
    """SQL and named ``queryParameters`` for daily cost of ``project_id`` in ``start``..``end``.

    ``start`` and ``end`` are inclusive UTC days. One output row per (day, location, SKU,
    service, currency).
    """
    validate_table(table)
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    sql = f"""
SELECT
  DATE(usage_start_time) AS day,
  COALESCE(location.region, location.location) AS location,
  sku.description AS sku,
  service.description AS service,
  currency,
  SUM(CAST(cost AS NUMERIC)) AS cost,
  SUM(IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) AS c), 0)) AS credits,
  SUM(CAST(usage.amount_in_pricing_units AS NUMERIC)) AS quantity
FROM `{table}`
WHERE _PARTITIONTIME >= TIMESTAMP(DATE_SUB(@start, INTERVAL {PARTITION_MARGIN_DAYS} DAY))
  AND usage_start_time >= TIMESTAMP(@start)
  AND usage_start_time < TIMESTAMP(DATE_ADD(@end, INTERVAL 1 DAY))
  AND project.id = @project
  AND service.description IN UNNEST(@services)
  AND IFNULL(cost_type, 'regular') != 'tax'
GROUP BY day, location, sku, service, currency
ORDER BY day, location, sku
""".strip()
    params = [
        _scalar("project", "STRING", project_id),
        _scalar("start", "DATE", start.isoformat()),
        _scalar("end", "DATE", end.isoformat()),
        {
            "name": "services",
            "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
            "parameterValue": {"arrayValues": [{"value": s} for s in SERVICES]},
        },
    ]
    return sql, params


async def pull_cost(
    client: Any, *, table: str, location: str, project_id: str, start: date, end: date
) -> list[CostRecord]:
    """Billed Vertex AI cost per UTC day, location and SKU for one project.

    The job runs in the export table's project (the service account holds
    ``roles/bigquery.jobUser`` there) and in the dataset's ``location``.
    """
    job_project = validate_table(table)[0]
    sql, params = build_query(table, project_id, start, end)
    body = {
        "query": sql,
        "useLegacySql": False,
        "parameterMode": "NAMED",
        "queryParameters": params,
        "location": location,
        "timeoutMs": POLL_TIMEOUT_MS,
    }
    page = await client.post(f"{BIGQUERY}/projects/{job_project}/queries", json=body)
    rows = [r async for r in _rows(client, job_project, location, page)]
    return [_record(project_id, r) for r in rows]


async def _rows(client: Any, job_project: str, location: str, page: dict[str, Any]):
    """Decoded rows across every page, polling while the job is still running.

    A failed job comes back as an HTTP error, which ``GcpClient`` raises; the response's
    ``errors`` list may hold warnings on success, so it isn't treated as failure.
    """
    ref = page.get("jobReference") or {}
    job_id = ref.get("jobId")
    job_location = ref.get("location") or location
    url = f"{BIGQUERY}/projects/{ref.get('projectId') or job_project}/queries/{job_id}"

    polls = 0
    while not page.get("jobComplete", False):
        polls += 1
        if not job_id or polls > MAX_POLLS:
            raise BillingQueryError(f"billing export query {job_id or '?'} did not complete")
        page = await client.get(
            url, params={"location": job_location, "timeoutMs": POLL_TIMEOUT_MS}
        )

    schema = page.get("schema") or {}
    while True:
        for row in page.get("rows") or []:
            yield decode_row(schema, row)
        token = page.get("pageToken")
        if not token:
            return
        if not job_id:
            raise BillingQueryError("paged billing export result without a jobReference")
        page = await client.get(
            url,
            params={"location": job_location, "pageToken": token, "timeoutMs": POLL_TIMEOUT_MS},
        )
        schema = page.get("schema") or schema


def decode_row(schema: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """BigQuery's ``{"f": [{"v": ...}]}`` row as ``{column: value}`` typed by the schema.

    Every scalar arrives as a string; NUMERIC and FLOAT become ``Decimal`` from that string
    (never via float), so the sum we store is the sum BigQuery computed.
    """
    fields = schema.get("fields") or []
    cells = row.get("f") or []
    if len(cells) != len(fields):
        raise BillingQueryError(f"row has {len(cells)} cells, schema has {len(fields)} fields")
    return {
        f["name"]: _decode(f.get("type", "STRING"), c.get("v"))
        for f, c in zip(fields, cells, strict=True)
    }


def _decode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    kind = kind.upper()
    if kind in _DECIMAL_TYPES:
        return Decimal(str(value))
    if kind in _INT_TYPES:
        return int(value)
    if kind == "DATE":
        return date.fromisoformat(value)
    if kind == "TIMESTAMP":
        # REST returns seconds since the epoch as a string, e.g. "1.7301024E9".
        return datetime.fromtimestamp(float(Decimal(str(value))), tz=UTC)
    if kind in {"BOOL", "BOOLEAN"}:
        return str(value).lower() == "true"
    return value


def _record(project_id: str, row: dict[str, Any]) -> CostRecord:
    cost = row.get("cost") or Decimal(0)
    credits = row.get("credits") or Decimal(0)
    return CostRecord(
        cloud="gcp",
        sub_account_id=project_id,
        resource_id=resource_id(project_id, row.get("location")),
        charge_period_start=row["day"],
        service_name=row.get("service"),
        sku_meter=row.get("sku") or "unknown",
        consumed_quantity=row.get("quantity"),
        billed_cost=cost + credits,
        billing_currency=row.get("currency") or "USD",
    )


def _scalar(name: str, kind: str, value: str) -> dict[str, Any]:
    return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": value}}
