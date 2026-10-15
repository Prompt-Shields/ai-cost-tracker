"""``act spike-azure``: check every Azure assumption marked "#2" against a live subscription.

The connector was written from Microsoft's REST docs. This tool replays the calls it makes
(plus a few probes, such as a 3-dimension grouping and a 13-month range) with the operator's
own ``az login`` and writes a Markdown report: each assumption confirmed, different (with
what we saw) or not checked (with why), sanitised sample payloads, and the constants to
change. Checks are isolated, so one failing call never hides the others' results.

Only read calls are made. The bearer token stays inside ``ArmClient``; nothing here reads
request headers, and the report is passed through ``Sanitiser`` before it is written.
"""

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx

from ai_cost_tracker.connectors.azure import _SUBSCRIPTIONS_QUERY as SUBSCRIPTIONS_QUERY
from ai_cost_tracker.connectors.azure import (
    AI_ACCOUNT_KINDS,
    DEPLOYMENTS_API_VERSION,
    RESOURCE_GRAPH_URL,
    cost_query,
    metrics,
)
from ai_cost_tracker.connectors.azure.client import _RETRY_HEADERS, ARM, ArmClient, Tokens

Status = Literal["confirmed", "different", "unchecked"]

METRIC_DEFINITIONS_API_VERSION = "2018-01-01"
DEFAULT_MODEL = "gpt-4o-mini"
CAMEL_RESOURCE_TYPES = [
    "Microsoft.CognitiveServices/accounts",
    "Microsoft.MachineLearningServices/workspaces",
]
# Fallbacks for constants owned by modules that may not be importable in every checkout.
_RETAIL_URL = "https://prices.azure.com/api/retail/prices"
_RETAIL_API_VERSION = "2023-01-01-preview"
_TOKEN_UNITS = {"1", "1k", "1m"}
_THROTTLE_HEADER = re.compile(r"ratelimit|retry-after|throttl", re.I)


# --------------------------------------------------------------------------- sanitiser

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_JWT = re.compile(r"eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+")
_SKIPTOKEN = re.compile(r"(?i)(\$?skiptoken=)[^&\s\"']+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_ARM_ID = re.compile(r"(?i)/subscriptions/[^\s\"'?#,;)]+")
_KIND_FOR_TYPE = {
    "accounts": "account",
    "deployments": "deployment",
    "workspaces": "workspace",
    "projects": "project",
}
# Values of these keys describe *what* a resource is, never *who*; they are never redacted.
_VOCABULARY_KEYS = {"kind", "location", "type", "format", "skuname", "metername", "unit"}


class Sanitiser:
    """Replaces subscription / tenant ids and resource names with stable placeholders.

    Names are learned from payloads (ARM ids, subscription rows) and then replaced wherever
    they appear, case-insensitively, because Cost Management lower-cases resource ids. Model,
    kind, SKU and region values are kept: they are the point of the report and not sensitive.
    """

    def __init__(self, subscription_id: str | None = None) -> None:
        self._guids: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._keep: set[str] = set()
        self._counts: Counter[str] = Counter()
        self._pattern: re.Pattern[str] | None = None
        if subscription_id:
            self._guids[subscription_id.lower()] = "{subscription-id}"

    def learn(self, obj: Any) -> None:
        """Collect names to redact (and vocabulary to keep) from a payload."""
        if isinstance(obj, str):
            for match in _ARM_ID.finditer(obj):
                self._learn_arm_id(match.group(0))
        elif isinstance(obj, list):
            for item in obj:
                self.learn(item)
        elif isinstance(obj, dict):
            self._learn_dict(obj)
            for value in obj.values():
                self.learn(value)

    def text(self, value: str) -> str:
        self.learn(value)
        value = _JWT.sub("{redacted-token}", value)
        value = _BEARER.sub("Bearer {redacted-token}", value)
        value = _SKIPTOKEN.sub(r"\1{redacted}", value)
        value = _EMAIL.sub("{email}", value)
        if self._names:
            if self._pattern is None:
                names = sorted(self._names, key=len, reverse=True)
                alternation = "|".join(re.escape(n) for n in names)
                self._pattern = re.compile(rf"(?<![\w-])(?:{alternation})(?![\w-])", re.I)
            value = self._pattern.sub(lambda m: self._names[m.group(0).lower()], value)
        return _GUID.sub(self._guid, value)

    def payload(self, obj: Any) -> Any:
        """Sanitised deep copy. Tag values are dropped (they often name people or teams)."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.payload(v) for v in obj]
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if key.lower() == "tags" and isinstance(value, dict):
                    out[key] = {k: "{tag-value}" for k in value}
                else:
                    out[key] = self.payload(value)
            return out
        return obj

    def _learn_dict(self, obj: dict[str, Any]) -> None:
        for key, value in obj.items():
            if isinstance(value, str) and key.lower() in _VOCABULARY_KEYS:
                self._keep.add(value.lower())
            if key.lower() == "tenantid" and isinstance(value, str) and _GUID.fullmatch(value):
                self._guids.setdefault(value.lower(), "{tenant-id}")
            if key.lower() == "sku" and isinstance(value, dict) and value.get("name"):
                self._keep.add(str(value["name"]).lower())
        # Deployment model objects: {"format": "OpenAI", "name": "gpt-4o", "version": ...}.
        if "format" in obj and isinstance(obj.get("name"), str):
            self._keep.add(obj["name"].lower())
        name = obj.get("name") or obj.get("displayName")
        is_resource = str(obj.get("id", "")).lower().startswith("/subscriptions/")
        if isinstance(name, str) and "subscriptionId" in obj and not is_resource:
            self._register(name, "subscription-name")

    def _learn_arm_id(self, arm_id: str) -> None:
        parts = [p for p in arm_id.split("/") if p]
        i = 0
        while i < len(parts):
            word = parts[i].lower()
            if word == "subscriptions":
                i += 2
            elif word == "resourcegroups" and i + 1 < len(parts):
                self._register(parts[i + 1], "resource-group")
                i += 2
            elif word == "providers":
                i += 2  # skip the namespace, then walk type/name pairs
                while i + 1 < len(parts) and parts[i].lower() != "providers":
                    kind = _KIND_FOR_TYPE.get(parts[i].lower(), "resource")
                    self._register(parts[i + 1], kind)
                    i += 2
            else:
                i += 1

    def _register(self, name: str, kind: str) -> None:
        low = name.lower()
        if len(low) < 3 or low in self._names or _GUID.fullmatch(low) or low.startswith("{"):
            return
        if low in self._keep:
            return
        self._counts[kind] += 1
        self._names[low] = f"{{{kind}-{self._counts[kind]}}}"
        self._pattern = None

    def _guid(self, match: re.Match[str]) -> str:
        low = match.group(0).lower()
        if low not in self._guids:
            self._counts["guid"] += 1
            self._guids[low] = f"{{guid-{self._counts['guid']}}}"
        return self._guids[low]


# --------------------------------------------------------------------------- report model


@dataclass
class Finding:
    key: str
    assumption: str
    where: str
    status: Status = "unchecked"
    observed: str = ""


@dataclass
class ConstantUpdate:
    file: str
    constant: str
    current: str
    observed: str


@dataclass
class CheckResult:
    name: str
    title: str
    notes: list[str] = field(default_factory=list)
    samples: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class HttpCall:
    method: str
    url: str
    status: int
    throttle_headers: dict[str, str]


@dataclass
class SpikeReport:
    subscription_id: str
    days: int
    started_at: datetime
    findings: dict[str, Finding]
    checks: list[CheckResult] = field(default_factory=list)
    constants: list[ConstantUpdate] = field(default_factory=list)
    calls: list[HttpCall] = field(default_factory=list)
    auth_error: str | None = None

    def counts(self) -> Counter[str]:
        return Counter(f.status for f in self.findings.values())


_CQ = "connectors/azure/cost_query.py"
_AZ = "connectors/azure/__init__.py"
_MX = "connectors/azure/metrics.py"
_CL = "connectors/azure/client.py"
_RT = "pricing/retail.py"

ASSUMPTIONS: list[tuple[str, str, str]] = [
    (
        "cost.column",
        "Pay-as-you-go rejects the `Cost` aggregation with a 400 and accepts `PreTaxCost` "
        "(the connector falls back once)",
        f"{_CQ}: DEFAULT_COST_COLUMN / PAYG_COST_COLUMN",
    ),
    (
        "cost.columns",
        "Response columns can be found by name: a cost column, a quantity column, UsageDate, "
        "ResourceId, Meter (parse_rows succeeds)",
        f"{_CQ}: _COST_COLUMNS / _QTY_COLUMNS",
    ),
    (
        "cost.usage_date",
        "UsageDate comes back as a yyyymmdd number for daily granularity",
        f"{_CQ}: _usage_date",
    ),
    ("cost.currency", "A Currency column is returned", f"{_CQ}: parse_rows"),
    (
        "cost.next_link",
        "Large results page via properties.nextLink (absolute ARM URL, POST the same body)",
        f"{_CQ}: next_link",
    ),
    (
        "cost.grouping",
        "The Query API allows at most two grouping dimensions (3 are rejected)",
        f"{_CQ}: build_query",
    ),
    (
        "cost.filter_case",
        "The ResourceType filter matches lower-case type names",
        f"{_CQ}: AI_RESOURCE_TYPES",
    ),
    (
        "cost.range",
        "Ranges over one year are rejected; a one-month range is accepted",
        f"{_CQ}: month_chunks",
    ),
    (
        "cost.throttle_headers",
        "Cost Management signals throttling with Retry-After or "
        "x-ms-ratelimit-microsoft.costmanagement-*-retry-after headers",
        f"{_CL}: _RETRY_HEADERS",
    ),
    (
        "graph.subscriptions",
        "The subscriptions query works and a small tenant fits in one page ($skipToken paging "
        "otherwise)",
        f"{_AZ}: _SUBSCRIPTIONS_QUERY / GRAPH_PAGE_SIZE",
    ),
    (
        "graph.kinds",
        "Only OpenAI / AIServices Cognitive Services accounts host model deployments",
        f"{_AZ}: AI_ACCOUNT_KINDS",
    ),
    (
        "graph.location",
        "Resource Graph account rows carry `location` (the region prices are matched on)",
        f"{_AZ}: _ACCOUNTS_QUERY",
    ),
    (
        "graph.deployment_type",
        "Resource Graph does not index deployments, so they are listed per account via ARM",
        f"{_AZ}: list_deployments",
    ),
    (
        "arm.deployments",
        "The deployments API version works and every sku.name is a known deployment type",
        f"{_AZ}: DEPLOYMENTS_API_VERSION; estimate.py: SKU_DEPLOYMENT_TYPE",
    ),
    (
        "monitor.metric_names",
        "Token metrics are named ProcessedPromptTokens / GeneratedTokens / AzureOpenAIRequests",
        f"{_MX}: INPUT_METRIC / OUTPUT_METRIC / REQUESTS_METRIC",
    ),
    (
        "monitor.dimension",
        "Token metrics can be split by the ModelDeploymentName dimension",
        f"{_MX}: DEPLOYMENT_DIMENSION",
    ),
    ("monitor.span", "A 7-day timespan is accepted in one metrics call", f"{_MX}: MAX_SPAN"),
    (
        "monitor.top",
        "top=1000 is accepted, so busy accounts are not capped at 10 series",
        f"{_MX}: MAX_SERIES",
    ),
    (
        "monitor.alignment",
        "PT1H buckets are aligned to clock hours when the timespan starts on the hour",
        f"{_MX}: time_chunks",
    ),
    (
        "prices.filter",
        "Model meters are under serviceName 'Cognitive Services' or 'Foundry Models' with a "
        "productName containing 'OpenAI' or 'Foundry', and $filter supports contains()",
        f"{_RT}: PRICE_FILTER",
    ),
    (
        "prices.units",
        "Token meters use unitOfMeasure '1K' or '1M' (or '1')",
        f"{_RT}: tokens_per_unit",
    ),
    (
        "prices.meters",
        "Retail meter names parse with the meter rules",
        "normalise/azure_meter_rules.json (parse_meter)",
    ),
]


def new_report(subscription_id: str, days: int, now: datetime | None = None) -> SpikeReport:
    findings = {k: Finding(k, a, w) for k, a, w in ASSUMPTIONS}
    return SpikeReport(subscription_id, days, now or datetime.now(UTC), findings)


# --------------------------------------------------------------------------- the spike


@dataclass
class Call:
    status: int
    payload: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None

    @property
    def denied(self) -> bool:
        return self.status in (401, 403)


@dataclass(frozen=True)
class Check:
    name: str
    title: str
    keys: tuple[str, ...]
    arm: bool = True


CHECKS = [
    Check(
        "cost_columns",
        "Cost Management query: `Cost` vs `PreTaxCost`",
        ("cost.column", "cost.columns", "cost.usage_date", "cost.currency", "cost.next_link"),
    ),
    Check("cost_grouping", "Cost Management query: three grouping dimensions", ("cost.grouping",)),
    Check("cost_filter_case", "Cost Management query: ResourceType casing", ("cost.filter_case",)),
    Check("cost_range", "Cost Management query: 13-month vs 1-month range", ("cost.range",)),
    Check("graph_subscriptions", "Resource Graph: subscriptions", ("graph.subscriptions",)),
    Check(
        "graph_accounts",
        "Resource Graph: AI accounts",
        ("graph.kinds", "graph.location", "graph.deployment_type"),
    ),
    Check("arm_deployments", "ARM: deployments of one account", ("arm.deployments",)),
    Check(
        "monitor_definitions",
        "Monitor: metric definitions",
        ("monitor.metric_names", "monitor.dimension"),
    ),
    Check(
        "monitor_metrics",
        "Monitor: token metrics call",
        ("monitor.span", "monitor.top", "monitor.alignment"),
    ),
    Check(
        "retail_prices",
        "Retail Prices API: one model",
        ("prices.filter", "prices.units", "prices.meters"),
        arm=False,
    ),
    Check("throttling", "Throttling headers seen", ("cost.throttle_headers",), arm=False),
]


class Spike:
    """Runs the checks in order; later checks reuse what earlier ones found (accounts...)."""

    def __init__(
        self,
        report: SpikeReport,
        tokens: Tokens,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        account: str | None = None,
        model: str = DEFAULT_MODEL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.report = report
        self.sub = report.subscription_id
        self.san = Sanitiser(self.sub)
        self.model = model
        self.account_override = account
        self.tokens = tokens
        hooks = {"response": [self._record]}
        http = httpx.AsyncClient(base_url=ARM, timeout=120, transport=transport, event_hooks=hooks)
        self.arm = ArmClient(tokens, http=http, sleep=sleep, max_retries=3)
        self.public = httpx.AsyncClient(timeout=60, transport=transport, event_hooks=hooks)
        today = report.started_at.date()
        self.end = today - timedelta(days=1)
        self.start = self.end - timedelta(days=report.days - 1)
        self.cost_column = cost_query.DEFAULT_COST_COLUMN
        self.cost_payload: dict[str, Any] | None = None
        self.accounts: list[dict[str, Any]] = []
        self.monitor_resource: str | None = account
        self.metric_defs: list[dict[str, Any]] = []

    async def run(self) -> SpikeReport:
        try:
            await self._preflight()
            for check in CHECKS:
                await self._run_check(check)
        finally:
            await self.arm.aclose()
            await self.public.aclose()
        return self.report

    async def _preflight(self) -> None:
        try:
            await self.tokens.token()  # value discarded: we only learn whether it works
        except Exception as exc:  # noqa: BLE001 - any credential failure means "run az login"
            self.report.auth_error = f"{type(exc).__name__}: {str(exc)[:400]}"

    async def _run_check(self, check: Check) -> None:
        result = CheckResult(check.name, check.title)
        self.report.checks.append(result)
        if check.arm and self.report.auth_error:
            result.error = "no Azure credential (run `az login`)"
        else:
            try:
                await getattr(self, f"check_{check.name}")(result)
            except Exception as exc:  # noqa: BLE001 - isolation: record and keep going
                result.error = f"{type(exc).__name__}: {str(exc)[:500]}"
        for key in check.keys:
            finding = self.report.findings[key]
            if not finding.observed:
                finding.observed = f"couldn't check: {result.error or 'not reached'}"

    # ---- helpers

    def _set(self, key: str, status: Status, observed: str) -> None:
        finding = self.report.findings[key]
        finding.status = status
        finding.observed = observed

    def _unchecked(self, key: str, why: str) -> None:
        self._set(key, "unchecked", f"couldn't check: {why}")

    def _constant(self, file: str, constant: str, current: Any, observed: Any) -> None:
        self.report.constants.append(ConstantUpdate(file, constant, str(current), str(observed)))

    async def _record(self, response: httpx.Response) -> None:
        headers = {k: v for k, v in response.headers.items() if _THROTTLE_HEADER.search(k)}
        url = str(response.request.url.copy_with(query=None))
        self.report.calls.append(
            HttpCall(response.request.method, url, response.status_code, headers)
        )

    async def _arm(
        self, method: str, url: str, *, json_body: dict | None = None, params: dict | None = None
    ) -> Call:
        try:
            payload = await self.arm.request(method, url, json=json_body, params=params)
        except httpx.HTTPStatusError as exc:
            return self._failed(exc.response)
        self.san.learn(payload)
        return Call(200, payload)

    async def _public(self, url: str, params: dict[str, str]) -> Call:
        resp = await self.public.get(url, params=params)
        if resp.is_error:
            return self._failed(resp)
        payload = resp.json()
        self.san.learn(payload)
        return Call(resp.status_code, payload)

    def _failed(self, resp: httpx.Response) -> Call:
        try:
            body = resp.json()
        except ValueError:
            body = {"text": resp.text[:300]}
        self.san.learn(body)
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        else:
            detail = json.dumps(body)[:300]
        return Call(resp.status_code, None, f"HTTP {resp.status_code} {detail[:400]}")

    def _cost(self, body: dict[str, Any]) -> Awaitable[Call]:
        return self._arm("POST", cost_query.query_url(self.sub), json_body=body)

    def _query(self, column: str | None = None, start: date | None = None) -> dict[str, Any]:
        return cost_query.build_query(start or self.start, self.end, column or self.cost_column)

    async def _graph(self, query: str, subscriptions: list[str] | None = None) -> Call:
        body: dict[str, Any] = {"query": query, "options": {"resultFormat": "objectArray"}}
        if subscriptions:
            body["subscriptions"] = subscriptions
        return await self._arm("POST", RESOURCE_GRAPH_URL, json_body=body)

    # ---- Cost Management

    async def check_cost_columns(self, r: CheckResult) -> None:
        calls = {}
        for column in (cost_query.DEFAULT_COST_COLUMN, cost_query.PAYG_COST_COLUMN):
            call = calls[column] = await self._cost(self._query(column))
            r.notes.append(f"`{column}`: " + ("HTTP 200" if call.ok else str(call.error)))
        cost, payg = calls[cost_query.DEFAULT_COST_COLUMN], calls[cost_query.PAYG_COST_COLUMN]
        if cost.denied and payg.denied:
            raise PermissionError(f"Cost Management denied both queries: {cost.error}")
        if not cost.ok and payg.ok:
            self.cost_column = cost_query.PAYG_COST_COLUMN
            self._set(
                "cost.column", "confirmed", f"`Cost` rejected ({cost.error}); `PreTaxCost` works"
            )
            self._constant(
                _CQ, "DEFAULT_COST_COLUMN", '"Cost"', '"PreTaxCost" (saves a 400 per sync)'
            )
        elif cost.ok:
            other = "also works" if payg.ok else f"is rejected ({payg.error})"
            self._set("cost.column", "different", f"`Cost` accepted; `PreTaxCost` {other}")
        else:
            self._set("cost.column", "different", f"both rejected: {cost.error} / {payg.error}")
            for key in ("cost.columns", "cost.usage_date", "cost.currency", "cost.next_link"):
                self._unchecked(key, "no cost query succeeded")
            return

        payload = self.cost_payload = (cost if cost.ok else payg).payload or {}
        props = payload.get("properties") or {}
        columns = props.get("columns") or []
        rows = props.get("rows") or []
        r.samples["cost query response (first rows)"] = payload
        r.notes.append(
            "columns: " + ", ".join(f"{c.get('name')} ({c.get('type')})" for c in columns)
        )
        r.notes.append(
            f"rows on first page: {len(rows)}; nextLink key present: {'nextLink' in props}"
        )
        names = [c.get("name") for c in columns]
        try:
            parsed = cost_query.parse_rows(self.sub, payload)
            self._set("cost.columns", "confirmed", f"parsed {len(parsed)} rows; columns {names}")
        except (ValueError, KeyError, TypeError) as exc:
            self._set("cost.columns", "different", f"parse_rows failed: {exc}; columns {names}")
            self._constant(
                _CQ, "_COST_COLUMNS / _QTY_COLUMNS", "see file", f"columns seen: {names}"
            )

        self._check_usage_date(columns, rows, names)
        if "Currency" in names:
            cur = sorted({str(row[names.index("Currency")]) for row in rows})
            self._set("cost.currency", "confirmed", f"Currency column present; values {cur or '-'}")
        else:
            self._set("cost.currency", "different", "no Currency column; parser assumes USD")

        link = props.get("nextLink")
        if not link:
            self._unchecked(
                "cost.next_link",
                f"one page only ({len(rows)} rows, nextLink={link!r}); rerun with more --days",
            )
            return
        host = httpx.URL(link).host
        page2 = await self._arm("POST", link, json_body=self._query())
        status: Status = "confirmed" if page2.ok and host == "management.azure.com" else "different"
        self._set(
            "cost.next_link", status, f"nextLink host {host}; page 2: {page2.error or 'HTTP 200'}"
        )

    def _check_usage_date(self, columns: list[dict], rows: list[list], names: list) -> None:
        if "UsageDate" not in names:
            self._set("cost.usage_date", "different", f"no UsageDate column; columns {names}")
            return
        i = names.index("UsageDate")
        col_type = columns[i].get("type")
        if not rows:
            self._unchecked("cost.usage_date", f"no rows in range (column type {col_type})")
            return
        value = rows[0][i]
        is_ymd = isinstance(value, int | float) and len(str(int(value))) == 8
        status: Status = "confirmed" if is_ymd else "different"
        observed = f"column type {col_type}; sample {value!r} ({type(value).__name__})"
        self._set("cost.usage_date", status, observed)

    async def check_cost_grouping(self, r: CheckResult) -> None:
        body = self._query()
        body["dataset"]["grouping"].append({"type": "Dimension", "name": "MeterSubCategory"})
        call = await self._cost(body)
        r.notes.append("grouping: ResourceId, Meter, MeterSubCategory")
        if call.denied:
            self._unchecked("cost.grouping", str(call.error))
        elif call.ok:
            cols = [
                c.get("name") for c in (call.payload or {}).get("properties", {}).get("columns", [])
            ]
            self._set("cost.grouping", "different", f"3 dimensions accepted; columns {cols}")
            r.samples["3-dimension response"] = call.payload
        else:
            self._set("cost.grouping", "confirmed", f"rejected: {call.error}")

    async def check_cost_filter_case(self, r: CheckResult) -> None:
        lower = self.cost_payload
        if lower is None:
            call = await self._cost(self._query())
            if not call.ok:
                self._unchecked("cost.filter_case", f"lower-case query failed: {call.error}")
                return
            lower = call.payload
        camel_body = self._query()
        camel_body["dataset"]["filter"]["dimensions"]["values"] = CAMEL_RESOURCE_TYPES
        camel = await self._cost(camel_body)
        if not camel.ok:
            self._unchecked("cost.filter_case", f"camel-case query failed: {camel.error}")
            return
        lo_rows, lo_total = self._summarise(lower or {})
        ca_rows, ca_total = self._summarise(camel.payload or {})
        r.notes.append(f"lower-case: {lo_rows} rows, total {lo_total} (first page)")
        r.notes.append(f"camel-case: {ca_rows} rows, total {ca_total} (first page)")
        seen = f"lower {lo_rows} rows / {lo_total}; camel {ca_rows} rows / {ca_total}"
        if lo_rows == 0 and ca_rows == 0:
            self._unchecked(
                "cost.filter_case", f"no AI cost in range; rerun with more --days ({seen})"
            )
        elif lo_rows == 0:
            self._set("cost.filter_case", "different", f"only camel-case matches: {seen}")
            self._constant(_CQ, "AI_RESOURCE_TYPES", "lower-case names", CAMEL_RESOURCE_TYPES)
        else:
            self._set("cost.filter_case", "confirmed", seen)

    def _summarise(self, payload: dict[str, Any]) -> tuple[int, Decimal]:
        records = cost_query.parse_rows(self.sub, payload)
        return len(records), sum((x.billed_cost for x in records), Decimal(0))

    async def check_cost_range(self, r: CheckResult) -> None:
        long = await self._cost(self._query(start=self.end - timedelta(days=395)))
        month = await self._cost(self._query(start=self.end - timedelta(days=30)))
        r.notes.append(f"13 months: {long.error or 'HTTP 200'}")
        r.notes.append(f"1 month: {month.error or 'HTTP 200'}")
        if long.denied or month.denied:
            self._unchecked("cost.range", str(long.error or month.error))
        elif not long.ok and month.ok:
            self._set("cost.range", "confirmed", f"13 months rejected ({long.error}); 1 month OK")
        else:
            seen = f"13 months: {long.error or 'accepted'}; 1 month: {month.error or 'accepted'}"
            self._set("cost.range", "different", seen)

    # ---- Resource Graph and ARM

    async def check_graph_subscriptions(self, r: CheckResult) -> None:
        call = await self._graph(SUBSCRIPTIONS_QUERY)
        if not call.ok:
            self._set(
                "graph.subscriptions",
                "different" if not call.denied else "unchecked",
                f"query failed: {call.error}",
            )
            return
        payload = call.payload or {}
        data = payload.get("data") or []
        r.samples["subscriptions query"] = payload
        mine = next(
            (d for d in data if str(d.get("subscriptionId", "")).lower() == self.sub.lower()), None
        )
        token = payload.get("$skipToken")
        r.notes.append(
            f"rows {len(data)}, totalRecords {payload.get('totalRecords')}, "
            f"resultTruncated {payload.get('resultTruncated')}, "
            f"$skipToken {'yes' if token else 'no'}"
        )
        paging = "one page"
        if token:
            body = {
                "query": SUBSCRIPTIONS_QUERY,
                "options": {"resultFormat": "objectArray", "$skipToken": token},
            }
            page2 = await self._arm("POST", RESOURCE_GRAPH_URL, json_body=body)
            paging = f"$skipToken page 2: {page2.error or 'HTTP 200'}"
        if mine is None:
            self._set(
                "graph.subscriptions",
                "different",
                f"subscription not in the {len(data)} rows ({paging})",
            )
        else:
            seen = (
                f"{len(data)} subscriptions, ours has aiAccounts={mine.get('aiAccounts')}; {paging}"
            )
            self._set("graph.subscriptions", "confirmed", seen)

    async def check_graph_accounts(self, r: CheckResult) -> None:
        types_q = (
            "resources | where type startswith 'microsoft.cognitiveservices/' or type startswith "
            "'microsoft.machinelearningservices/' | summarize count() by type, kind"
        )
        types = await self._graph(types_q, [self.sub])
        accounts_q = (
            "resources | where type =~ 'microsoft.cognitiveservices/accounts' "
            "| project id, name, kind, location, sku, tags | order by id asc"
        )
        accounts = await self._graph(accounts_q, [self.sub])
        if types.ok:
            rows = (types.payload or {}).get("data") or []
            r.notes.append(
                "types/kinds: "
                + "; ".join(
                    f"{x.get('type')} kind={x.get('kind') or '-'} x{x.get('count_')}" for x in rows
                )
                or "none"
            )
            dep_rows = [x for x in rows if str(x.get("type", "")).lower().endswith("/deployments")]
            if dep_rows:
                n = sum(int(x.get("count_") or 0) for x in dep_rows)
                self._set(
                    "graph.deployment_type",
                    "different",
                    f"Resource Graph indexes deployments ({n}); inventory could use one query",
                )
            else:
                self._set(
                    "graph.deployment_type", "confirmed", "no */deployments type in Resource Graph"
                )
        else:
            self._unchecked("graph.deployment_type", str(types.error))
        if not accounts.ok:
            self._unchecked("graph.kinds", str(accounts.error))
            self._unchecked("graph.location", str(accounts.error))
            return
        self.accounts = (accounts.payload or {}).get("data") or []
        r.samples["accounts query"] = accounts.payload
        kinds = Counter(str(a.get("kind")) for a in self.accounts)
        r.notes.append(f"Cognitive Services accounts by kind: {dict(kinds) or 'none'}")
        known = {k.strip(" '").lower() for k in AI_ACCOUNT_KINDS.strip("()").split(",")}
        others = sorted(k for k in kinds if k.lower() not in known)
        # Final verdict on graph.kinds comes from arm_deployments (do other kinds have any?).
        self._set(
            "graph.kinds",
            "confirmed",
            f"kinds {dict(kinds) or 'none'}; no other kinds present"
            if not others
            else f"kinds {dict(kinds)}; other kinds {others} not probed yet",
        )
        locations = [a.get("location") for a in self.accounts]
        if not self.accounts:
            self._unchecked("graph.location", "no Cognitive Services accounts in the subscription")
        elif all(locations):
            self._set("graph.location", "confirmed", f"locations {sorted(set(locations))}")
        else:
            self._set("graph.location", "different", f"some rows lack location: {locations}")

    def _ai_accounts(self) -> list[dict[str, Any]]:
        known = {k.strip(" '").lower() for k in AI_ACCOUNT_KINDS.strip("()").split(",")}
        return [a for a in self.accounts if str(a.get("kind", "")).lower() in known]

    async def check_arm_deployments(self, r: CheckResult) -> None:
        ai = self._ai_accounts()
        target = self.account_override or (ai[0]["id"] if ai else None)
        if target is None:
            self._unchecked("arm.deployments", "no OpenAI / AIServices account found")
            return
        best: tuple[int, str] | None = None
        for acct in [target] + [a["id"] for a in ai if a["id"] != target][:4]:
            call = await self._arm(
                "GET", f"{acct}/deployments?api-version={DEPLOYMENTS_API_VERSION}"
            )
            if acct == target:
                if not call.ok:
                    status: Status = "unchecked" if call.denied else "different"
                    self._set("arm.deployments", status, f"list failed: {call.error}")
                    return
                self._deployments_finding(r, call.payload or {})
            n = len((call.payload or {}).get("value") or [])
            if call.ok and (best is None or n > best[0]):
                best = (n, acct)
        if self.monitor_resource is None and best:
            self.monitor_resource = best[1]
        await self._probe_other_kinds(r)

    def _deployments_finding(self, r: CheckResult, payload: dict[str, Any]) -> None:
        items = payload.get("value") or []
        r.samples["deployments list"] = payload
        skus = Counter(str((i.get("sku") or {}).get("name")) for i in items)
        models = sorted(
            {
                f"{(i.get('properties') or {}).get('model', {}).get('format')}/"
                f"{(i.get('properties') or {}).get('model', {}).get('name')}"
                for i in items
            }
        )
        r.notes.append(f"{len(items)} deployments; sku names {dict(skus)}; models {models}")
        r.notes.append(f"nextLink present: {bool(payload.get('nextLink'))}")
        known = _known_skus()
        unknown = sorted(s for s in skus if s.replace(" ", "").lower() not in known)
        if unknown:
            self._set(
                "arm.deployments", "different", f"unknown sku names {unknown}; seen {dict(skus)}"
            )
            self._constant("estimate.py", "SKU_DEPLOYMENT_TYPE", "see file", f"add {unknown}")
        else:
            self._set(
                "arm.deployments", "confirmed", f"{len(items)} deployments; sku names {dict(skus)}"
            )

    async def _probe_other_kinds(self, r: CheckResult) -> None:
        ai_ids = {a["id"] for a in self._ai_accounts()}
        others = [a for a in self.accounts if a["id"] not in ai_ids][:3]
        with_deployments = []
        for acct in others:
            call = await self._arm(
                "GET", f"{acct['id']}/deployments?api-version={DEPLOYMENTS_API_VERSION}"
            )
            n = len((call.payload or {}).get("value") or [])
            r.notes.append(f"kind {acct.get('kind')}: {call.error or f'{n} deployments'}")
            if n:
                with_deployments.append(str(acct.get("kind")))
        if with_deployments:
            self._set(
                "graph.kinds",
                "different",
                f"kinds {sorted(set(with_deployments))} also have deployments",
            )
            self._constant(
                _AZ, "AI_ACCOUNT_KINDS", AI_ACCOUNT_KINDS, f"add {sorted(set(with_deployments))}"
            )
        elif others:
            kinds = sorted({str(a.get("kind")) for a in others})
            self._set("graph.kinds", "confirmed", f"other kinds {kinds} have no deployments")

    # ---- Azure Monitor

    async def check_monitor_definitions(self, r: CheckResult) -> None:
        rid = self.monitor_resource
        if not rid:
            for key in ("monitor.metric_names", "monitor.dimension"):
                self._unchecked(key, "no OpenAI / AIServices account found")
            return
        url = f"{rid}/providers/Microsoft.Insights/metricDefinitions"
        call = await self._arm("GET", url, params={"api-version": METRIC_DEFINITIONS_API_VERSION})
        if not call.ok:
            for key in ("monitor.metric_names", "monitor.dimension"):
                self._unchecked(key, str(call.error))
            return
        defs = self.metric_defs = (call.payload or {}).get("value") or []
        r.samples["metricDefinitions (first entries)"] = call.payload
        for d in defs:
            name = _metric_name(d)
            dims = ", ".join((x.get("value") or "") for x in d.get("dimensions") or []) or "-"
            line = f"`{name}` ({d.get('unit')}, {d.get('primaryAggregationType')}): {dims}"
            r.notes.append(f"**{line}** (token-related)" if "token" in name.lower() else line)
        by_name = {_metric_name(d).lower(): d for d in defs}
        configured = [metrics.INPUT_METRIC, metrics.OUTPUT_METRIC, metrics.REQUESTS_METRIC]
        missing = [m for m in configured if m.lower() not in by_name]
        token_names = [_metric_name(d) for d in defs if "token" in _metric_name(d).lower()]
        if missing:
            self._set(
                "monitor.metric_names",
                "different",
                f"missing {missing}; token metrics available: {token_names}",
            )
            hints = {
                "INPUT_METRIC": ("prompt", "input"),
                "OUTPUT_METRIC": ("generated", "completion", "output"),
                "REQUESTS_METRIC": ("request",),
            }
            for const, words in hints.items():
                if getattr(metrics, const) in missing:
                    names = [_metric_name(d) for d in defs]
                    pool = token_names if const != "REQUESTS_METRIC" else names
                    cands = [n for n in pool if any(w in n.lower() for w in words)]
                    self._constant(
                        _MX, const, getattr(metrics, const), cands or "no candidate seen"
                    )
        else:
            self._set(
                "monitor.metric_names", "confirmed", f"all present; token metrics: {token_names}"
            )
        present = [by_name[m.lower()] for m in configured if m.lower() in by_name]
        # If the configured names are wrong, the dimension question still applies to whatever
        # token metrics exist.
        present = present or [d for d in defs if "token" in _metric_name(d).lower()]
        lacking = [
            _metric_name(d)
            for d in present
            if metrics.DEPLOYMENT_DIMENSION.lower()
            not in {(x.get("value") or "").lower() for x in d.get("dimensions") or []}
        ]
        if not present:
            self._unchecked("monitor.dimension", "no configured or token metrics exist")
        elif lacking:
            dims = sorted({x.get("value") for d in present for x in d.get("dimensions") or []})
            self._set(
                "monitor.dimension", "different", f"{lacking} lack it; dimensions seen {dims}"
            )
        else:
            self._set(
                "monitor.dimension", "confirmed", f"present on {[_metric_name(d) for d in present]}"
            )

    async def check_monitor_metrics(self, r: CheckResult) -> None:
        rid = self.monitor_resource
        if not rid:
            for key in ("monitor.span", "monitor.top", "monitor.alignment"):
                self._unchecked(key, "no OpenAI / AIServices account found")
            return
        now = self.report.started_at.replace(minute=0, second=0, microsecond=0)
        path, params = metrics.metrics_request(rid, now - metrics.MAX_SPAN, now)
        call = await self._arm("GET", path, params=params)
        r.notes.append(
            f"configured metrics over {metrics.MAX_SPAN.days} days: {call.error or 'HTTP 200'}"
        )
        if not call.ok and self.metric_defs:
            names = [
                n for n in (_metric_name(d) for d in self.metric_defs) if "token" in n.lower()
            ][:5]
            if names:
                params = {**params, "metricnames": ",".join(names)}
                call = await self._arm("GET", path, params=params)
                r.notes.append(f"retry with {names}: {call.error or 'HTTP 200'}")
        span_ok = call.ok
        if not call.ok:
            day = {**params, "timespan": f"{_iso(now - timedelta(days=1))}/{_iso(now)}"}
            short = await self._arm("GET", path, params=day)
            r.notes.append(f"same call over 1 day: {short.error or 'HTTP 200'}")
            if short.ok:
                self._set("monitor.span", "different", f"7 days rejected ({call.error}); 1 day OK")
                self._constant(_MX, "MAX_SPAN", "timedelta(days=7)", "timedelta(days=1)")
                call = short
            else:
                for key in ("monitor.span", "monitor.top", "monitor.alignment"):
                    self._unchecked(key, f"metrics call failed: {call.error}")
                return
        if span_ok:
            self._set(
                "monitor.span",
                "confirmed",
                f"7-day timespan accepted (timespan {call.payload.get('timespan')})",
            )
        self._metrics_findings(r, call.payload or {}, params)

    def _metrics_findings(self, r: CheckResult, payload: dict[str, Any], params: dict) -> None:
        r.samples["metrics response (first series)"] = payload
        series = Counter()
        deployments: set[str] = set()
        stamps: list[str] = []
        for m in payload.get("value") or []:
            for ts in m.get("timeseries") or []:
                series[(m.get("name") or {}).get("value")] += 1
                for md in ts.get("metadatavalues") or []:
                    deployments.add(str(md.get("value")))
                stamps.extend(p.get("timeStamp", "") for p in ts.get("data") or [])
        r.notes.append(
            f"series per metric: {dict(series) or 'none'}; deployments seen: {len(deployments)}"
        )
        r.notes.append(
            f"interval {payload.get('interval')}; filter {params.get('$filter')}; "
            f"top {params.get('top')}"
        )
        capped = any(n == 10 for n in series.values())
        status: Status = "different" if capped else "confirmed"
        note = " (exactly 10 series: looks capped)" if capped else ""
        self._set(
            "monitor.top", status, f"top={params.get('top')} accepted; series {dict(series)}{note}"
        )
        if not stamps:
            self._unchecked("monitor.alignment", "no data points returned")
            return
        off = [s for s in stamps if not re.search(r"T\d{2}:00:00", s)]
        if off:
            self._set(
                "monitor.alignment",
                "different",
                f"{len(off)} of {len(stamps)} off the hour, e.g. {off[0]}",
            )
        else:
            self._set(
                "monitor.alignment",
                "confirmed",
                f"{len(stamps)} points, all on the hour (first {stamps[0]})",
            )

    # ---- Retail Prices API (public; no token is sent)

    async def check_retail_prices(self, r: CheckResult) -> None:
        url, version, price_filter = _retail_constants()
        ours = f"({price_filter}) and contains(meterName, '{self.model}')"
        broad = f"priceType eq 'Consumption' and contains(meterName, '{self.model}')"
        a = await self._public(url, {"api-version": version, "$filter": ours})
        b = await self._public(url, {"api-version": version, "$filter": broad})
        r.notes.append(f"our filter: {a.error or f'{len(_items(a))} items'}")
        r.notes.append(f"broad filter: {b.error or f'{len(_items(b))} items'}")
        if a.ok:
            r.samples["retail prices (our filter)"] = a.payload
        items = _items(a) or _items(b)
        combos_a = {(i.get("serviceName"), i.get("productName")) for i in _items(a)}
        combos_b = {(i.get("serviceName"), i.get("productName")) for i in _items(b)}
        r.notes.append(f"service/product seen: {sorted(map(str, combos_b | combos_a))}")
        if not a.ok:
            self._set("prices.filter", "different", f"our filter rejected: {a.error}")
        elif not _items(a):
            self._set(
                "prices.filter",
                "different",
                f"no items for {self.model}; broad filter saw {sorted(map(str, combos_b))}",
            )
        elif missed := combos_b - combos_a:
            self._set("prices.filter", "different", f"filter misses {sorted(map(str, missed))}")
            self._constant(
                _RT, "PRICE_FILTER", "see file", f"also include {sorted(map(str, missed))}"
            )
        else:
            self._set(
                "prices.filter",
                "confirmed",
                f"{len(_items(a))} items; {sorted(map(str, combos_a))}",
            )
        if not items:
            self._unchecked("prices.units", "no items")
            self._unchecked("prices.meters", "no items")
            return
        units = Counter(str(i.get("unitOfMeasure")) for i in items)
        meters = sorted({str(i.get("meterName")) for i in items})
        r.notes.append(f"unitOfMeasure: {dict(units)}")
        r.notes.append(f"meter names: {meters[:20]}")
        token_units = {
            str(i.get("unitOfMeasure")) for i in items if "token" in str(i.get("meterName")).lower()
        }
        odd = sorted(u for u in token_units if u.replace(" ", "").lower() not in _TOKEN_UNITS)
        if odd:
            self._set(
                "prices.units",
                "different",
                f"token meters with unit {odd}; all units {dict(units)}",
            )
        else:
            self._set("prices.units", "confirmed", f"units {dict(units)}")
        from ai_cost_tracker.normalise.azure_meters import parse_meter

        unparsed = [m for m in meters if parse_meter(m) is None]
        if unparsed:
            self._set(
                "prices.meters",
                "different",
                f"{len(unparsed)}/{len(meters)} unparsed: {unparsed[:10]}",
            )
            self._constant(
                "normalise/azure_meter_rules.json",
                "families / known_models",
                "see file",
                f"unparsed {unparsed[:10]}",
            )
        else:
            self._set("prices.meters", "confirmed", f"all {len(meters)} meter names parse")

    # ---- throttling headers (from every Cost Management response recorded so far)

    async def check_throttling(self, r: CheckResult) -> None:
        cm = [c for c in self.report.calls if "microsoft.costmanagement" in c.url.lower()]
        seen = Counter(h.lower() for c in cm for h in c.throttle_headers)
        throttled = sum(1 for c in cm if c.status == 429)
        r.notes.append(f"{len(cm)} Cost Management responses, {throttled} with HTTP 429")
        r.notes.append(f"headers seen: {dict(seen) or 'none'}")
        for c in cm[:1]:
            r.samples["throttling headers on first response"] = c.throttle_headers
        if not cm:
            self._unchecked("cost.throttle_headers", "no Cost Management responses")
            return
        ours = {h.lower() for h in _RETRY_HEADERS}
        retry_like = {h for h in seen if "retry-after" in h}
        extra = sorted(retry_like - ours)
        if extra:
            self._set(
                "cost.throttle_headers",
                "different",
                f"unlisted retry headers {extra}; all {sorted(seen)}",
            )
            self._constant(_CL, "_RETRY_HEADERS", "see file", f"add {extra}")
        elif retry_like:
            self._set("cost.throttle_headers", "confirmed", f"seen {sorted(seen)}")
        elif seen:
            self._unchecked(
                "cost.throttle_headers",
                f"never throttled, so no retry-after header; quota headers seen {sorted(seen)}",
            )
        else:
            self._unchecked(
                "cost.throttle_headers", f"no throttling headers in {len(cm)} responses"
            )


def _metric_name(definition: dict[str, Any]) -> str:
    return str((definition.get("name") or {}).get("value") or "")


def _items(call: Call) -> list[dict[str, Any]]:
    return (call.payload or {}).get("Items") or [] if call.ok else []


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _known_skus() -> set[str]:
    try:
        from ai_cost_tracker.estimate import SKU_DEPLOYMENT_TYPE

        return set(SKU_DEPLOYMENT_TYPE)
    except ImportError:  # pragma: no cover - estimate.py ships with M1
        return {"standard", "globalstandard", "datazonestandard", "provisionedmanaged"}


def _retail_constants() -> tuple[str, str, str]:
    try:
        from ai_cost_tracker.pricing import retail

        return retail.RETAIL_PRICES_URL, retail.API_VERSION, retail.PRICE_FILTER
    except (ImportError, AttributeError):  # pragma: no cover - retail.py ships with M1
        return _RETAIL_URL, _RETAIL_API_VERSION, "serviceName eq 'Cognitive Services'"


async def run_spike(
    subscription_id: str,
    days: int = 3,
    *,
    tokens: Tokens | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    account: str | None = None,
    model: str = DEFAULT_MODEL,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[SpikeReport, Sanitiser]:
    """Run every check; ``tokens`` defaults to ``DefaultAzureCredential`` (``az login``)."""
    if tokens is None:
        from ai_cost_tracker.connectors.azure.client import TokenProvider

        tokens = TokenProvider()
    report = new_report(subscription_id, days, now)
    spike = Spike(report, tokens, transport=transport, account=account, model=model, sleep=sleep)
    await spike.run()
    return report, spike.san


# --------------------------------------------------------------------------- rendering

_LABEL = {"confirmed": "confirmed", "different": "**different**", "unchecked": "couldn't check"}


def trim(obj: Any, max_items: int = 8, max_text: int = 300) -> Any:
    """Shorten sample payloads: long lists of objects or rows keep their first items (a row
    itself is kept whole), long strings are cut.
    """
    if isinstance(obj, list):
        if not any(isinstance(v, list | dict) for v in obj):
            return [trim(v, max_items, max_text) for v in obj]
        head = [trim(v, max_items, max_text) for v in obj[:max_items]]
        return head + ([f"... {len(obj) - max_items} more"] if len(obj) > max_items else [])
    if isinstance(obj, dict):
        return {k: trim(v, max_items, max_text) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > max_text:
        return obj[:max_text] + "..."
    return obj


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(report: SpikeReport, san: Sanitiser | None = None) -> str:
    """Markdown report; every string goes through the sanitiser."""
    san = san or Sanitiser(report.subscription_id)
    s = san.text
    counts = report.counts()
    out = [
        "# Azure pay-as-you-go spike (issue #2)",
        "",
        f"Generated by `act spike-azure` on {report.started_at:%Y-%m-%d %H:%M} UTC against "
        f"subscription `{s(report.subscription_id)}`, cost range the last {report.days} "
        f"complete days. Identifiers are replaced by placeholders such as `{{account-1}}`.",
        "",
        f"**{counts['confirmed']} confirmed, {counts['different']} different, "
        f"{counts['unchecked']} couldn't check.**",
        "",
    ]
    if report.auth_error:
        out += [f"> No Azure credential: {s(report.auth_error)}. Run `az login` and retry.", ""]
    out += [
        "## Assumptions",
        "",
        "| # | Assumption | Where | Result | Observed |",
        "|---|---|---|---|---|",
    ]
    for i, f in enumerate(report.findings.values(), 1):
        out.append(
            f"| {i} | {_cell(f.assumption)} | `{f.where}` | {_LABEL[f.status]} "
            f"| {_cell(s(f.observed))} |"
        )
    out += ["", "## Constants to update", ""]
    if report.constants:
        out += ["| File | Constant | Current | Observed |", "|---|---|---|---|"]
        for c in report.constants:
            out.append(
                f"| `{c.file}` | `{c.constant}` | {_cell(s(c.current))} | {_cell(s(c.observed))} |"
            )
    else:
        out.append('None: nothing observed calls for a change (see "couldn\'t check" rows above).')
    out += ["", "## Checks", ""]
    for i, check in enumerate(report.checks, 1):
        out += [f"### {i}. {check.title}", ""]
        if check.error:
            out += [f"Error: `{_cell(s(check.error))}`", ""]
        out += [f"- {s(n)}" for n in check.notes]
        for name, sample in check.samples.items():
            body = json.dumps(trim(san.payload(sample)), indent=2, default=str)
            out += [
                "",
                f"<details><summary>{name}</summary>",
                "",
                "```json",
                body,
                "```",
                "",
                "</details>",
            ]
        out.append("")
    out += [
        "## HTTP calls",
        "",
        "| Method | URL | Status | Throttling headers |",
        "|---|---|---|---|",
    ]
    for c in report.calls:
        headers = ", ".join(f"{k}: {v}" for k, v in c.throttle_headers.items()) or "-"
        out.append(f"| {c.method} | `{_cell(s(c.url))}` | {c.status} | {_cell(s(headers))} |")
    return "\n".join(out) + "\n"


def summary_lines(report: SpikeReport, san: Sanitiser) -> list[str]:
    """One line per assumption for the terminal."""
    return [f"[{f.status:>10}] {f.key}: {san.text(f.observed)}" for f in report.findings.values()]
