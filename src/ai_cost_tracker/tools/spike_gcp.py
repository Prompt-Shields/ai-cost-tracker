"""``act spike-gcp``: check the GCP connector's ``Assumption (spike-gcp)`` notes on a live project.

The GCP modules were written from Google's public REST docs. This tool authenticates the
way production does (WIF, ``auth.py``), calls the real connector functions where they exist
and a few raw endpoints otherwise, and writes a short Markdown report: one row per check
(pass / fail / skip) plus trimmed, sanitised samples. Checks are isolated, so one failure
never hides the others.

Read calls only, except one small BigQuery query job. Tokens never leave ``GcpClient`` /
``WifTokenProvider``; the report keeps project ids but drops tokens, emails, billing account
ids and page tokens.
"""

import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from ai_cost_tracker.config import Settings

Status = Literal["pass", "fail", "skip"]
CheckFn = Callable[[], Awaitable[tuple[Status, str, Any]]]

CRM = "https://cloudresourcemanager.googleapis.com/v3"
SERVICE_USAGE = "https://serviceusage.googleapis.com/v1"
MONITORING = "https://monitoring.googleapis.com/v3"
BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"
BILLING_CATALOG = "https://cloudbilling.googleapis.com/v1"
METRIC_PREFIX = "aiplatform.googleapis.com/publisher/online_serving/"
TOKEN_METRIC = METRIC_PREFIX + "token_count"
_TABLE = re.compile(r"^([\w-]+)\.([\w-]+)\.([\w-]+)$")
# Assumption (spike-gcp): the detailed (resource-level) export has these columns.
EXPECTED_BILLING_COLUMNS = (
    "usage_start_time",
    "project",
    "service",
    "sku",
    "location",
    "cost",
    "currency",
    "credits",
    "usage",
    "cost_type",
)
MAX_SKUS = 3000
# One slow or endlessly paginating call must not stall the remaining checks.
CHECK_TIMEOUT = 180.0

# --------------------------------------------------------------------------- sanitising

_JWT = re.compile(r"eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_GOOGLE_TOKEN = re.compile(r"ya29\.[\w.-]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+")
_PAGE_TOKEN = re.compile(r"(?i)(page_?token[\"'=:\s]+)[^&\s\"',}]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# Billing account ids: 012345-6789AB-CDEF01, or with underscores inside export table names.
_BILLING_ACCOUNT = re.compile(r"(?i)\b[0-9a-f]{6}[-_][0-9a-f]{6}[-_][0-9a-f]{6}\b")
_BILLING_ACCOUNT_IN_TABLE = re.compile(r"(?i)(_v1_)[0-9a-f]{6}_[0-9a-f]{6}_[0-9a-f]{6}")


def sanitise(text: str) -> str:
    """Remove tokens, emails, billing account ids and page tokens; project ids stay."""
    text = _JWT.sub("{redacted-token}", text)
    text = _GOOGLE_TOKEN.sub("{redacted-token}", text)
    text = _BEARER.sub("Bearer {redacted-token}", text)
    text = _PAGE_TOKEN.sub(r"\1{redacted}", text)
    text = _EMAIL.sub("{email}", text)
    text = _BILLING_ACCOUNT_IN_TABLE.sub(r"\1{billing-account}", text)
    return _BILLING_ACCOUNT.sub("{billing-account}", text)


def sanitise_payload(obj: Any) -> Any:
    """Sanitised copy; label/tag values are dropped (they often name people or teams)."""
    if isinstance(obj, str):
        return sanitise(obj)
    if isinstance(obj, list):
        return [sanitise_payload(v) for v in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            low = key.lower()
            if low in {"labels", "tags"} and isinstance(value, dict):
                out[key] = {k: "{value}" for k in value}
            elif low in {"billing_account_id", "billingaccountname"}:
                out[key] = "{billing-account}"
            else:
                out[key] = sanitise_payload(value)
        return out
    return obj


def trim(obj: Any, max_items: int = 5, max_text: int = 300) -> Any:
    if isinstance(obj, list):
        head = [trim(v, max_items, max_text) for v in obj[:max_items]]
        return head + ([f"... {len(obj) - max_items} more"] if len(obj) > max_items else [])
    if isinstance(obj, dict):
        return {k: trim(v, max_items, max_text) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > max_text:
        return obj[:max_text] + "..."
    return obj


# --------------------------------------------------------------------------- report


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    sample: Any = None


@dataclass
class Report:
    project_id: str
    started_at: datetime
    checks: list[Check] = field(default_factory=list)

    async def run(self, name: str, fn: CheckFn) -> None:
        try:
            status, detail, sample = await asyncio.wait_for(fn(), CHECK_TIMEOUT)
        except TimeoutError:
            status, detail, sample = "fail", f"no answer within {CHECK_TIMEOUT:.0f} s", None
        except Exception as exc:  # isolate checks; record what went wrong
            status, detail, sample = "fail", f"{type(exc).__name__}: {exc}", None
        self.checks.append(Check(name, status, detail, sample))


def render(report: Report) -> str:
    counts = {s: sum(c.status == s for c in report.checks) for s in ("pass", "fail", "skip")}
    out = [
        "# GCP spike (issue #46)",
        "",
        f"Generated by `act spike-gcp` on {report.started_at:%Y-%m-%d %H:%M} UTC against project "
        f"`{report.project_id}`. Tokens, emails and billing account ids are redacted.",
        "",
        f"**{counts['pass']} pass, {counts['fail']} fail, {counts['skip']} skipped.**",
        "",
        "| # | Check | Result | Detail |",
        "|---|---|---|---|",
    ]
    for i, c in enumerate(report.checks, 1):
        detail = sanitise(c.detail).replace("|", "\\|").replace("\n", " ")
        out.append(f"| {i} | {c.name} | {c.status} | {detail} |")
    out += ["", "## Samples", ""]
    for c in report.checks:
        if c.sample is None:
            continue
        body = json.dumps(trim(sanitise_payload(c.sample)), indent=2, default=str)
        out += [f"<details><summary>{c.name}</summary>", "", "```json", body, "```", ""]
        out += ["</details>", ""]
    return sanitise("\n".join(out)) + "\n"


# --------------------------------------------------------------------------- checks


def _optional(module: str) -> ModuleType | None:
    """A sibling worker's module, or None when it isn't importable in this checkout."""
    try:
        return importlib.import_module(module)
    except Exception:
        return None


def _func(module: ModuleType | None, name: str) -> Callable[..., Any] | None:
    fn = getattr(module, name, None) if module else None
    return fn if callable(fn) else None


async def _call_module(fn: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
    """Await a module function; ``NotImplementedError`` (a stub) counts as not available."""
    if fn is None:
        raise LookupError("module function not available")
    try:
        return await fn(*args, **kwargs)
    except NotImplementedError:
        raise LookupError("module function not implemented yet") from None


class _Spike:
    def __init__(self, settings: Settings, project_id: str, client: Any, report: Report) -> None:
        self.s = settings
        self.p = project_id
        self.c = client
        self.r = report
        self.yesterday = report.started_at.date() - timedelta(days=1)

    async def run(self) -> None:
        r = self.r
        await r.run("WIF token exchange", self.token)
        await r.run("Project get (Resource Manager v3)", self.project)
        await r.run("inventory.check_access", self.check_access)
        await r.run("Service Usage: aiplatform.googleapis.com state", self.service_usage)
        await r.run(f"Metric descriptors {METRIC_PREFIX}*", self.descriptors)
        await r.run("token_count time series (1 day): label keys", self.time_series)
        await r.run("monitoring.pull_usage (1 day)", self.pull_usage)
        for region in self.s.gcp_regions:
            await r.run(f"Vertex AI endpoints list ({region})", lambda g=region: self.endpoints(g))
        await r.run("inventory.list_deployments", self.list_deployments)
        await r.run("Billing export table schema (tables.get)", self.table_schema)
        await r.run("Billing export: services seen in 1 day", self.billing_services)
        await r.run("billing.pull_cost (1 day)", self.pull_cost)
        await r.run("Cloud Billing catalog: Vertex AI SKUs", self.catalog)

    async def token(self) -> tuple[Status, str, Any]:
        tokens = getattr(self.c, "tokens", None)
        if tokens is None or not hasattr(tokens, "token"):
            return "skip", "client has no token provider to check", None
        value = await tokens.token()
        return "pass", f"got a Google access token ({len(value)} chars)", None

    async def project(self) -> tuple[Status, str, Any]:
        data = await self.c.get(f"{CRM}/projects/{self.p}")
        detail = f"state={data.get('state')}, displayName={data.get('displayName')!r}"
        return ("pass" if data.get("state") == "ACTIVE" else "fail"), detail, data

    async def check_access(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.gcp.inventory"), "check_access")
        try:
            ok = await _call_module(fn, self.c, self.p)
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        return ("pass" if ok else "fail"), f"check_access returned {ok}", None

    async def service_usage(self) -> tuple[Status, str, Any]:
        # Assumption (spike-gcp): a project that uses Vertex AI has this service ENABLED.
        data = await self.c.get(
            f"{SERVICE_USAGE}/projects/{self.p}/services/aiplatform.googleapis.com"
        )
        state = data.get("state")
        return ("pass" if state == "ENABLED" else "fail"), f"state={state}", data

    async def descriptors(self) -> tuple[Status, str, Any]:
        params = {"filter": f'metric.type = starts_with("{METRIC_PREFIX}")'}
        data = await self.c.get(f"{MONITORING}/projects/{self.p}/metricDescriptors", params)
        found = {
            d.get("type", "").removeprefix(METRIC_PREFIX): sorted(
                lbl.get("key", "") for lbl in d.get("labels", [])
            )
            for d in data.get("metricDescriptors", [])
        }
        if not found:
            return "fail", "no publisher online_serving metric descriptors", None
        has = "token_count" in found and "model_invocation_count" in found
        detail = "metrics: " + ", ".join(sorted(found))
        return ("pass" if has else "fail"), detail, found

    async def time_series(self) -> tuple[Status, str, Any]:
        # Assumption (spike-gcp): token_count carries metric label `type` (input/output) and
        # resource labels `location` and `model_user_id`.
        start = datetime.combine(self.yesterday, time(), UTC)
        params = {
            "filter": f'metric.type = "{TOKEN_METRIC}"',
            "interval.startTime": _iso(start),
            "interval.endTime": _iso(start + timedelta(days=1)),
            "aggregation.alignmentPeriod": "3600s",
            "aggregation.perSeriesAligner": "ALIGN_SUM",
            "view": "FULL",
        }
        data = await self.c.get(f"{MONITORING}/projects/{self.p}/timeSeries", params)
        series = data.get("timeSeries", [])
        if not series:
            return "skip", f"no token_count series on {self.yesterday} (no traffic?)", None
        metric_keys = sorted({k for s in series for k in s.get("metric", {}).get("labels", {})})
        resource = series[0].get("resource", {})
        resource_keys = sorted({k for s in series for k in s.get("resource", {}).get("labels", {})})
        types = sorted({s.get("metric", {}).get("labels", {}).get("type", "") for s in series})
        ok = "type" in metric_keys and {"location", "model_user_id"} <= set(resource_keys)
        detail = (
            f"{len(series)} series; resource.type={resource.get('type')}; metric labels "
            f"{metric_keys}; resource labels {resource_keys}; type values {types}"
        )
        sample = {**series[0], "points": series[0].get("points", [])[:2]}
        return ("pass" if ok else "fail"), detail, sample

    async def pull_usage(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.gcp.monitoring"), "pull_usage")
        start = datetime.combine(self.yesterday, time(), UTC)
        try:
            rows = await _call_module(fn, self.c, self.p, [], start, start + timedelta(days=1))
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        models = sorted({getattr(r, "model", None) or "?" for r in rows})
        return "pass", f"{len(rows)} rows; models {models}", None

    async def endpoints(self, region: str) -> tuple[Status, str, Any]:
        # Assumption (spike-gcp): regional endpoints live on <region>-aiplatform.googleapis.com,
        # the global location on aiplatform.googleapis.com.
        host = (
            "aiplatform.googleapis.com"
            if region == "global"
            else f"{region}-aiplatform.googleapis.com"
        )
        url = f"https://{host}/v1/projects/{self.p}/locations/{region}/endpoints"
        data = await self.c.get(url)
        eps = data.get("endpoints", [])
        deployed = sum(len(e.get("deployedModels", [])) for e in eps)
        return "pass", f"{len(eps)} endpoints, {deployed} deployed models", eps or None

    async def list_deployments(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.gcp.inventory"), "list_deployments")
        try:
            deps = await _call_module(fn, self.c, self.p, list(self.s.gcp_regions))
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        names = [f"{getattr(d, 'name', '?')} -> {getattr(d, 'model', '?')}" for d in deps]
        return "pass", f"{len(deps)} deployments: {names[:10]}", None

    def _table(self) -> tuple[str, str, str] | None:
        m = _TABLE.match(self.s.gcp_billing_export_table or "")
        return (m.group(1), m.group(2), m.group(3)) if m else None

    async def table_schema(self) -> tuple[Status, str, Any]:
        if not (parts := self._table()):
            return (
                "skip",
                "ACT_GCP_BILLING_EXPORT_TABLE not set (or not project.dataset.table)",
                None,
            )
        proj, ds, tbl = parts
        data = await self.c.get(f"{BIGQUERY}/projects/{proj}/datasets/{ds}/tables/{tbl}")
        fields = {f.get("name"): f for f in data.get("schema", {}).get("fields", [])}
        missing = [c for c in EXPECTED_BILLING_COLUMNS if c not in fields]
        nested = {
            name: [sub.get("name") for sub in fields[name].get("fields", [])]
            for name in ("service", "sku", "credits", "usage", "location", "project")
            if name in fields
        }
        detail = (
            f"`{proj}.{ds}.{tbl}`: "
            + (f"missing columns {missing}" if missing else "all expected columns present")
            + f"; partitioning={data.get('timePartitioning', {}).get('type')}"
        )
        return ("fail" if missing else "pass"), detail, nested

    async def billing_services(self) -> tuple[Status, str, Any]:
        # Assumption (spike-gcp): Vertex AI model usage is billed under service.description
        # "Vertex AI" (Gemini API / partner models may use other services).
        if not (parts := self._table()):
            return "skip", "ACT_GCP_BILLING_EXPORT_TABLE not set", None
        proj, ds, tbl = parts
        sql = (
            f"SELECT service.description AS service, COUNT(*) AS n, SUM(cost) AS cost, "
            f"ANY_VALUE(currency) AS currency FROM `{proj}.{ds}.{tbl}` "
            "WHERE project.id = @project AND DATE(usage_start_time) = @day "
            "AND DATE(_PARTITIONTIME) BETWEEN @day AND DATE_ADD(@day, INTERVAL 5 DAY) "
            "GROUP BY service ORDER BY cost DESC LIMIT 20"
        )
        body = {
            "query": sql,
            "useLegacySql": False,
            "location": self.s.gcp_billing_export_location,
            "parameterMode": "NAMED",
            "queryParameters": [
                _param("project", "STRING", self.p),
                _param("day", "DATE", self.yesterday.isoformat()),
            ],
            "timeoutMs": 30000,
        }
        data = await self.c.post(f"{BIGQUERY}/projects/{proj}/queries", json=body)
        if not data.get("jobComplete"):
            return "skip", "query did not finish within 30 s; rerun", None
        rows = [[cell.get("v") for cell in row.get("f", [])] for row in data.get("rows", [])]
        services = [r[0] for r in rows]
        status: Status = "pass" if "Vertex AI" in services else "fail"
        return status, f"services on {self.yesterday}: {services}", rows

    async def pull_cost(self) -> tuple[Status, str, Any]:
        if not self._table():
            return "skip", "ACT_GCP_BILLING_EXPORT_TABLE not set", None
        fn = _func(_optional("ai_cost_tracker.connectors.gcp.billing"), "pull_cost")
        try:
            rows = await _call_module(
                fn,
                self.c,
                table=self.s.gcp_billing_export_table,
                location=self.s.gcp_billing_export_location,
                project_id=self.p,
                start=self.yesterday,
                end=self.yesterday,
            )
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        skus = sorted({getattr(r, "sku_meter", "?") for r in rows})
        return "pass", f"{len(rows)} cost rows; SKUs {skus[:10]}", None

    async def catalog(self) -> tuple[Status, str, Any]:
        # Assumption (spike-gcp): the catalog lists the service with displayName "Vertex AI".
        notes = []
        if _optional("ai_cost_tracker.pricing.gcp_catalog") is None:
            notes.append("pricing.gcp_catalog not available")
        service_id = await self._vertex_service_id()
        if service_id is None:
            return "fail", "no 'Vertex AI' service in the catalog", None
        descriptions = await self._sku_descriptions(service_id)
        from ai_cost_tracker.normalise import parse_meter

        unparsed = sorted(d for d in descriptions if parse_meter("gcp", d) is None)
        notes.append(
            f"service {service_id}; {len(descriptions)} SKUs, {len(unparsed)} not parsed by "
            "normalise.parse_meter"
        )
        sample = {
            "parsed": sorted(set(descriptions) - set(unparsed))[:30],
            "unparsed": unparsed[:60],
        }
        return "pass", "; ".join(notes), sample

    async def _vertex_service_id(self) -> str | None:
        params: dict[str, str] = {"pageSize": "5000"}
        while True:
            data = await self.c.get(f"{BILLING_CATALOG}/services", params)
            for svc in data.get("services", []):
                if svc.get("displayName") == "Vertex AI":
                    return svc.get("serviceId")
            if not (page := data.get("nextPageToken")):
                return None
            params = {**params, "pageToken": page}

    async def _sku_descriptions(self, service_id: str) -> list[str]:
        params: dict[str, str] = {"currencyCode": "USD", "pageSize": "5000"}
        out: list[str] = []
        while len(out) < MAX_SKUS:
            data = await self.c.get(f"{BILLING_CATALOG}/services/{service_id}/skus", params)
            out += [s.get("description", "") for s in data.get("skus", [])]
            if not (page := data.get("nextPageToken")):
                break
            params = {**params, "pageToken": page}
        return out


def _param(name: str, kind: str, value: str) -> dict[str, Any]:
    return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": value}}


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_spike(
    settings: Settings,
    project_id: str,
    out: Path,
    *,
    client: Any = None,
    now: datetime | None = None,
) -> Path:
    """Run every check against ``project_id`` and write the sanitised report to ``out``.

    ``client`` defaults to a WIF-authenticated ``GcpClient`` from ``settings`` (closed here);
    a supplied client is left open for its owner.
    """
    report = Report(project_id=project_id, started_at=now or datetime.now(UTC))
    owned = client is None
    if owned:
        from ai_cost_tracker.connectors.gcp import client_from_settings

        client = client_from_settings(settings)
    try:
        await _Spike(settings, project_id, client, report).run()
    finally:
        if owned:
            await client.aclose()
    await asyncio.to_thread(_write, out, render(report))
    return out


def _write(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def default_out(project_id: str, today: date | None = None) -> Path:
    """Suggested report path for the CLI: ``spike-gcp-<project>-<date>.md``."""
    return Path(f"spike-gcp-{project_id}-{(today or date.today()).isoformat()}.md")


__all__ = ["Check", "Report", "default_out", "render", "run_spike", "sanitise"]
