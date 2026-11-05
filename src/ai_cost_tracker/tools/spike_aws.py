"""``act spike-aws``: check the AWS connector's ``Assumption (spike-aws)`` notes on a live account.

The AWS modules were written from AWS's public docs. This tool authenticates the way
production does (Azure managed identity -> ``AssumeRoleWithWebIdentity``, through
``AwsSessions``), calls the real connector functions where they exist and a few raw APIs
otherwise, and writes a short Markdown report: one row per check (pass / fail / skip) plus
trimmed, sanitised samples. Checks are isolated, so one failure never hides the others.

Read calls only. Credentials never leave ``AwsSessions``; the report keeps account ids and
region names but drops credentials, tokens, emails, tag values, page tokens and role
session names.
"""

import asyncio
import csv
import importlib
import io
import json
import re
import zlib
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import httpx

from ai_cost_tracker.config import Settings

Status = Literal["pass", "fail", "skip"]
CheckFn = Callable[[], Awaitable[tuple[Status, str, Any]]]

NAMESPACE = "AWS/Bedrock"
# Assumption (spike-aws): Bedrock publishes these per-ModelId metrics in AWS/Bedrock.
TOKEN_METRICS = ("InputTokenCount", "OutputTokenCount", "Invocations")
PRICE_LIST = "https://pricing.us-east-1.amazonaws.com"
OFFER_INDEX = f"{PRICE_LIST}/offers/v1.0/aws/index.json"
BEDROCK_REGION_INDEX = f"{PRICE_LIST}/offers/v1.0/aws/AmazonBedrock/current/region_index.json"
# Assumption (spike-aws): CUR 2.0 columns we read, used when billing.CUR_COLUMNS isn't there.
FALLBACK_CUR_COLUMNS = (
    "line_item_usage_start_date",
    "line_item_usage_account_id",
    "line_item_product_code",
    "line_item_line_item_type",
    "line_item_usage_type",
    "line_item_usage_amount",
    "line_item_unblended_cost",
    "line_item_currency_code",
    "product_region_code",
    "product_servicecode",
)
_PERIOD = re.compile(r"/BILLING_PERIOD=(\d{4}-\d{2})/")
MAX_ITEMS = 2000  # per listing; enough to see shapes without walking huge accounts
MAX_PAGES = 50
MAX_CUR_BYTES = 8 * 1024 * 1024  # compressed bytes read from one data file
MAX_CUR_TEXT = 32 * 1024 * 1024  # decompressed bytes kept
# One slow or endlessly paginating call must not stall the remaining checks.
CHECK_TIMEOUT = 180.0

# --------------------------------------------------------------------------- sanitising

_JWT = re.compile(r"eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+")
_PAGE_TOKEN = re.compile(r"(?i)((?:next|continuation|page)_?token[\"'=:\s]+)[^&\s\"',}]+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# arn:aws:sts::<acct>:assumed-role/<role>/<session> -> keep the role, drop the session name.
_ASSUMED_ROLE = re.compile(r"(assumed-role/[\w+=,.@-]+)/[\w+=,.@-]+")
# GetCallerIdentity UserId: <role unique id>:<session name>.
_ROLE_USER_ID = re.compile(r"\bAROA[A-Z0-9]{8,}:[\w+=,.@-]+")
_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{12,}\b")
_SESSION_TOKEN = re.compile(r"\b(?:IQoJ|FwoG|FQoG)[\w/+=]{16,}")
_SECRET_KEY = re.compile(r"(?<![\w/+=])[A-Za-z0-9/+]{40}(?![\w/+=])")
_SECRET_KEYS = ("token", "secret", "credential", "password", "sessionname", "accesskey")


def sanitise(text: str) -> str:
    """Remove credentials, tokens, emails, page tokens and session names; account ids stay."""
    text = _JWT.sub("{redacted-token}", text)
    text = _BEARER.sub("Bearer {redacted-token}", text)
    text = _SESSION_TOKEN.sub("{redacted-token}", text)
    text = _PAGE_TOKEN.sub(r"\1{redacted}", text)
    text = _EMAIL.sub("{email}", text)
    text = _ASSUMED_ROLE.sub(r"\1/{session}", text)
    text = _ROLE_USER_ID.sub("{role-id}:{session}", text)
    text = _ACCESS_KEY.sub("{redacted-key}", text)
    return _SECRET_KEY.sub("{redacted-secret}", text)


def sanitise_payload(obj: Any) -> Any:
    """Sanitised copy; secret-looking keys and tag values are dropped (tags name people)."""
    if isinstance(obj, str):
        return sanitise(obj)
    if isinstance(obj, list):
        return [sanitise_payload(v) for v in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            low = str(key).lower()
            if any(s in low for s in _SECRET_KEYS):
                out[key] = "{redacted}"
            elif low == "email":
                out[key] = "{email}"
            elif low in {"tags", "labels"}:
                out[key] = _tag_keys(value)
            else:
                out[key] = sanitise_payload(value)
        return out
    return obj


def _tag_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {sanitise(str(k)): "{value}" for k in value}
    if isinstance(value, list):
        return [
            {
                k: ("{value}" if str(k).lower() == "value" else sanitise_payload(v))
                for k, v in item.items()
            }
            if isinstance(item, dict)
            else "{value}"
            for item in value
        ]
    return "{value}"


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
    account_id: str
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

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, "skip", detail))


def render(report: Report) -> str:
    counts = {s: sum(c.status == s for c in report.checks) for s in ("pass", "fail", "skip")}
    out = [
        "# AWS spike (issue #51)",
        "",
        f"Generated by `act spike-aws` on {report.started_at:%Y-%m-%d %H:%M} UTC against account "
        f"`{report.account_id}`. Credentials, tokens, emails, tag values and role session names "
        "are redacted.",
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


# --------------------------------------------------------------------------- helpers


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


def _cur_columns() -> tuple[tuple[str, ...], str]:
    cols = getattr(_optional("ai_cost_tracker.connectors.aws.billing"), "CUR_COLUMNS", None)
    if isinstance(cols, (tuple, list)) and cols:
        return tuple(cols), "billing.CUR_COLUMNS"
    return FALLBACK_CUR_COLUMNS, "fallback list (billing.CUR_COLUMNS not available)"


def _is_bedrock(row: dict[str, str]) -> bool:
    # Assumption (spike-aws): first-party rows have product code AmazonBedrock; Marketplace
    # models carry their own product code with "(Amazon Bedrock Edition)" in the name.
    for col in (
        "line_item_product_code",
        "product_servicecode",
        "product_product_name",
        "line_item_line_item_description",
    ):
        if "bedrock" in (row.get(col) or "").lower():
            return True
    return False


@dataclass
class _CurSample:
    key: str
    header: list[str]
    rows: list[dict[str, str]]
    truncated: bool


class _Spike:
    def __init__(
        self,
        settings: Settings,
        account_id: str,
        sessions: Any,
        http: httpx.AsyncClient,
        report: Report,
    ) -> None:
        self.s = settings
        self.a = account_id
        self.sessions = sessions
        self.http = http
        self.r = report
        self.regions = list(settings.aws_regions) or ["us-east-1"]
        self.yesterday = report.started_at.date() - timedelta(days=1)
        self.model_ids: dict[str, list[str]] = {}
        self.export = self._export()
        self.data_keys: list[str] = []
        self.manifest_keys: list[str] = []
        self._cur: _CurSample | None = None

    def _export(self) -> Any:
        mod = _optional("ai_cost_tracker.connectors.aws")
        fn = _func(mod, "export_from_settings")
        return fn(self.s) if fn else None

    async def run(self) -> None:
        aws = self._aws
        await aws("Azure token + AssumeRoleWithWebIdentity", self.assume_role)
        await aws("STS GetCallerIdentity", self.caller_identity)
        await aws("inventory.check_access", self.check_access)
        for g in self.regions:
            await aws(f"Bedrock ListFoundationModels ({g})", lambda g=g: self.foundation(g))
            await aws(f"Bedrock ListInferenceProfiles ({g})", lambda g=g: self.profiles(g))
            await aws(
                f"Bedrock ListProvisionedModelThroughputs ({g})", lambda g=g: self.provisioned(g)
            )
            await aws(f"CloudWatch ListMetrics {NAMESPACE} ({g})", lambda g=g: self.metrics(g))
            await aws(f"CloudWatch GetMetricData 1 day ({g})", lambda g=g: self.metric_data(g))
        await aws(f"metrics.seen_model_ids ({self.regions[0]})", self.seen_model_ids)
        await aws("metrics.pull_usage (1 day)", self.pull_usage)
        await aws("inventory.list_deployments", self.list_deployments)
        await aws("inventory.list_accounts", self.list_accounts)
        await aws("Organizations ListAccounts (export account)", self.org_accounts)
        await aws("S3 export prefix layout", self.export_layout)
        await aws("Export manifest (latest period)", self.manifest)
        await aws("CUR data file header vs CUR_COLUMNS", self.cur_header)
        await aws("CUR Bedrock usage types parsed", self.cur_usage_types)
        await aws("billing.pull_cost (1 day)", self.pull_cost)
        await self.r.run("Price list: offer index lists Bedrock offers", self.offer_index)
        await self.r.run("Price list: AmazonBedrock region_index.json", self.region_index)
        await self.r.run(
            f"Price list: usage types parsed ({self.regions[0]})", self.price_usage_types
        )

    async def _aws(self, name: str, fn: CheckFn) -> None:
        if self.sessions is None:
            self.r.skip(name, "no AWS sessions (see 'AWS sessions' check)")
        else:
            await self.r.run(name, fn)

    async def _pages(
        self,
        client: Any,
        method: str,
        result_key: str,
        *,
        token_in: str = "NextToken",
        token_out: str | None = None,
        max_items: int = MAX_ITEMS,
        **kwargs: Any,
    ) -> tuple[list[Any], bool]:
        """Items and whether the listing was cut short; a repeated page token is an error."""
        items: list[Any] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(MAX_PAGES):
            extra = {token_in: token} if token else {}
            data = await self.sessions.call(client, method, **kwargs, **extra)
            items += data.get(result_key) or []
            token = data.get(token_out or token_in)
            if not token:
                return items, False
            if len(items) >= max_items:
                return items[:max_items], True
            if token in seen:
                raise RuntimeError(f"{method} returned a page token it already returned")
            seen.add(token)
        return items, True

    # ------------------------------------------------------------------ identity

    async def assume_role(self) -> tuple[Status, str, Any]:
        await self.sessions.client("sts", self.a, self.regions[0])
        role = self.s.aws_role_name
        return "pass", f"temporary credentials for role `{role}` in `{self.a}`", None

    async def caller_identity(self) -> tuple[Status, str, Any]:
        sts = await self.sessions.client("sts", self.a, self.regions[0])
        data = await self.sessions.call(sts, "get_caller_identity")
        arn = sanitise(str(data.get("Arn", "")))
        ok = data.get("Account") == self.a and f"assumed-role/{self.s.aws_role_name}/" in arn
        return (
            ("pass" if ok else "fail"),
            f"Account={data.get('Account')}, Arn={arn}",
            {"Account": data.get("Account"), "Arn": arn},
        )

    async def check_access(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.aws.inventory"), "check_access")
        try:
            ok = await _call_module(fn, self.sessions, self.a)
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        return ("pass" if ok else "fail"), f"check_access returned {ok}", None

    # ------------------------------------------------------------------ bedrock

    async def foundation(self, region: str) -> tuple[Status, str, Any]:
        client = await self.sessions.client("bedrock", self.a, region)
        data = await self.sessions.call(client, "list_foundation_models")
        models = data.get("modelSummaries") or []
        providers = sorted({m.get("providerName", "?") for m in models})
        ids = sorted(m.get("modelId", "") for m in models)
        status: Status = "pass" if models else "fail"
        return status, f"{len(models)} foundation models; providers {providers}", ids

    async def profiles(self, region: str) -> tuple[Status, str, Any]:
        client = await self.sessions.client("bedrock", self.a, region)
        out: dict[str, Any] = {}
        for kind in ("SYSTEM_DEFINED", "APPLICATION"):
            items, cut = await self._pages(
                client,
                "list_inference_profiles",
                "inferenceProfileSummaries",
                token_in="nextToken",
                typeEquals=kind,
            )
            out[kind] = {
                "count": len(items),
                "truncated": cut,
                "ids": sorted(p.get("inferenceProfileId", "") for p in items),
                "fields": sorted({k for p in items for k in p}),
            }
        detail = ", ".join(f"{k}: {v['count']}" for k, v in out.items())
        return "pass", detail, out

    async def provisioned(self, region: str) -> tuple[Status, str, Any]:
        client = await self.sessions.client("bedrock", self.a, region)
        items, _ = await self._pages(
            client,
            "list_provisioned_model_throughputs",
            "provisionedModelSummaries",
            token_in="nextToken",
        )
        statuses = dict(Counter(p.get("status", "?") for p in items))
        sample = [
            {
                k: p.get(k)
                for k in ("provisionedModelName", "foundationModelArn", "modelUnits", "status")
            }
            for p in items
        ]
        detail = f"{len(items)} provisioned throughputs; status {statuses}"
        return "pass", detail, sample or None

    # ------------------------------------------------------------------ cloudwatch

    async def metrics(self, region: str) -> tuple[Status, str, Any]:
        client = await self.sessions.client("cloudwatch", self.a, region)
        items, cut = await self._pages(client, "list_metrics", "Metrics", Namespace=NAMESPACE)
        names = sorted({m.get("MetricName", "") for m in items})
        dims = sorted({d.get("Name", "") for m in items for d in m.get("Dimensions", [])})
        model_ids = sorted(
            {
                d.get("Value", "")
                for m in items
                if m.get("MetricName") == "InputTokenCount"
                for d in m.get("Dimensions", [])
                if d.get("Name") == "ModelId"
            }
        )
        self.model_ids[region] = model_ids
        if not items:
            return "skip", "no AWS/Bedrock metrics in this region (no traffic?)", None
        missing = [n for n in TOKEN_METRICS if n not in names]
        ok = not missing and "ModelId" in dims
        detail = (
            f"{len(items)} metrics{' (truncated)' if cut else ''}; names {names}; "
            f"dimensions {dims}; {len(model_ids)} ModelIds with InputTokenCount"
            + (f"; missing {missing}" if missing else "")
        )
        return ("pass" if ok else "fail"), detail, {"model_ids": model_ids, "metrics": names}

    async def metric_data(self, region: str) -> tuple[Status, str, Any]:
        ids = self.model_ids.get(region) or []
        if not ids:
            return "skip", "no ModelId with InputTokenCount from ListMetrics", None
        model_id = ids[0]
        client = await self.sessions.client("cloudwatch", self.a, region)
        start = datetime.combine(self.yesterday, time(), UTC)
        queries = [
            {
                "Id": f"m{i}",
                "Label": name,
                "MetricStat": {
                    "Metric": {
                        "Namespace": NAMESPACE,
                        "MetricName": name,
                        "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            }
            for i, name in enumerate(TOKEN_METRICS)
        ]
        results, _ = await self._pages(
            client,
            "get_metric_data",
            "MetricDataResults",
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=start + timedelta(days=1),
        )
        by_id = {q["Id"]: q["Label"] for q in queries}
        totals = {
            by_id.get(r.get("Id", ""), r.get("Id")): (
                sum(r.get("Values") or []),
                len(r.get("Values") or []),
                r.get("StatusCode"),
            )
            for r in results
        }
        has_data = any(n for _, n, _ in totals.values())
        detail = f"`{model_id}` on {self.yesterday}: " + ", ".join(
            f"{k} sum={s:g} over {n} hours ({code})" for k, (s, n, code) in totals.items()
        )
        sample = [{**r, "Timestamps": (r.get("Timestamps") or [])[:2]} for r in results]
        return ("pass" if has_data else "skip"), detail, sample

    async def seen_model_ids(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.aws.metrics"), "seen_model_ids")
        try:
            ids = await _call_module(fn, self.sessions, self.a, self.regions[0])
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        return "pass", f"{len(ids)} ModelIds: {sorted(ids)[:10]}", None

    async def pull_usage(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.aws.metrics"), "pull_usage")
        start = datetime.combine(self.yesterday, time(), UTC)
        try:
            rows = await _call_module(
                fn, self.sessions, self.a, self.regions, [], start, start + timedelta(days=1)
            )
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        models = sorted({getattr(r, "model", None) or "?" for r in rows})
        return "pass", f"{len(rows)} rows; models {models}", None

    async def list_deployments(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.aws.inventory"), "list_deployments")
        try:
            deps = await _call_module(fn, self.sessions, self.a, self.regions)
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        names = [
            f"{getattr(d, 'name', '?')} -> {getattr(d, 'model', '?')} ({getattr(d, 'sku', None)})"
            for d in deps
        ]
        return "pass", f"{len(deps)} deployments: {names[:10]}", None

    async def list_accounts(self) -> tuple[Status, str, Any]:
        fn = _func(_optional("ai_cost_tracker.connectors.aws.inventory"), "list_accounts")
        try:
            accounts = await _call_module(
                fn,
                self.sessions,
                include_all=False,
                only={self.a},
                export_account_id=self.export.account_id if self.export else None,
                regions=self.regions,
            )
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        rows = [
            f"{getattr(x, 'account_id', '?')} ai={getattr(x, 'has_ai_resources', '?')}"
            for x in accounts
        ]
        return "pass", f"{len(accounts)} accounts: {rows[:10]}", None

    # ------------------------------------------------------------------ export account

    def _no_export(self) -> tuple[Status, str, Any]:
        return "skip", "ACT_AWS_EXPORT_ACCOUNT_ID / ACT_AWS_EXPORT_BUCKET not set", None

    async def org_accounts(self) -> tuple[Status, str, Any]:
        if self.export is None:
            return self._no_export()
        # Organizations is a global API served from us-east-1.
        client = await self.sessions.client("organizations", self.export.account_id, "us-east-1")
        items, cut = await self._pages(client, "list_accounts", "Accounts")
        statuses = dict(Counter(a.get("Status", "?") for a in items))
        listed = self.a in {a.get("Id") for a in items}
        detail = (
            f"{len(items)} accounts{' (truncated)' if cut else ''}; status {statuses}; "
            f"`{self.a}` {'listed' if listed else 'not listed'}"
        )
        sample = [{k: a.get(k) for k in ("Id", "Status", "JoinedMethod")} for a in items]
        return ("pass" if items else "fail"), detail, sample

    async def _s3(self) -> Any:
        return await self.sessions.client("s3", self.export.account_id, self.export.region)

    def _base(self) -> str:
        return "/".join(p for p in (self.export.prefix.strip("/"), self.export.name) if p) + "/"

    async def export_layout(self) -> tuple[Status, str, Any]:
        # Assumption (spike-aws): <prefix>/<name>/data/BILLING_PERIOD=YYYY-MM/*.csv.gz and
        # <prefix>/<name>/metadata/BILLING_PERIOD=YYYY-MM/*Manifest.json (overwrite mode).
        if self.export is None:
            return self._no_export()
        s3 = await self._s3()
        base = self._base()
        items, cut = await self._pages(
            s3,
            "list_objects_v2",
            "Contents",
            token_in="ContinuationToken",
            token_out="NextContinuationToken",
            Bucket=self.export.bucket,
            Prefix=base,
        )
        keys = [o.get("Key", "") for o in items]
        if not keys:
            return (
                "fail",
                f"nothing under s3://{self.export.bucket}/{base} (not delivered yet?)",
                None,
            )
        self.data_keys = [k for k in keys if k.startswith(base + "data/") and _PERIOD.search(k)]
        self.manifest_keys = [
            k
            for k in keys
            if k.startswith(base + "metadata/") and k.lower().endswith("manifest.json")
        ]
        periods = sorted({m.group(1) for k in self.data_keys if (m := _PERIOD.search(k))})
        suffixes = dict(Counter(_suffix(k) for k in self.data_keys))
        other = [k for k in keys if k not in self.data_keys and k not in self.manifest_keys]
        ok = bool(self.data_keys) and bool(self.manifest_keys) and set(suffixes) == {".csv.gz"}
        detail = (
            f"{len(keys)} objects{' (truncated)' if cut else ''}; periods {periods}; data files "
            f"{suffixes}; {len(self.manifest_keys)} manifests; {len(other)} other objects"
        )
        sample = {
            "data": self.data_keys[-5:],
            "manifests": self.manifest_keys[-5:],
            "other": other[:10],
        }
        return ("pass" if ok else "fail"), detail, sample

    async def manifest(self) -> tuple[Status, str, Any]:
        if self.export is None:
            return self._no_export()
        if not self.manifest_keys:
            return "skip", "no manifest found by the layout check", None
        key = max(self.manifest_keys, key=_period_key)
        s3 = await self._s3()
        data = await self.sessions.call(s3, "get_object", Bucket=self.export.bucket, Key=key)
        raw = await _read_body(data.get("Body"), 4 * 1024 * 1024)
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict):
            return "fail", f"`{key}` is not a JSON object", None
        lists = {k: len(v) for k, v in doc.items() if isinstance(v, list)}
        detail = f"`{key}`: top-level keys {sorted(doc)}; list sizes {lists}"
        sample = {k: v for k, v in doc.items() if k.lower() not in {"columns"}}
        return "pass", detail, sample

    async def _cur_sample(self) -> _CurSample | None:
        if self._cur is not None or not self.data_keys:
            return self._cur
        csvs = [k for k in self.data_keys if k.endswith(".csv.gz")]
        if not csvs:
            return None
        key = sorted(csvs, key=_period_key)[-1]
        s3 = await self._s3()
        data = await self.sessions.call(
            s3,
            "get_object",
            Bucket=self.export.bucket,
            Key=key,
            Range=f"bytes=0-{MAX_CUR_BYTES - 1}",
        )
        text, truncated = await _gunzip_head(data.get("Body"))
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        rows = [dict(zip(header, r, strict=False)) for r in reader if len(r) == len(header)]
        self._cur = _CurSample(key=key, header=header, rows=rows, truncated=truncated)
        return self._cur

    async def cur_header(self) -> tuple[Status, str, Any]:
        if self.export is None:
            return self._no_export()
        if not self.data_keys:
            return "skip", "no data files found by the layout check", None
        sample = await self._cur_sample()
        if sample is None:
            return "fail", "no .csv.gz data file (export not CSV + GZIP?)", None
        expected, source = _cur_columns()
        missing = [c for c in expected if c not in sample.header]
        detail = f"`{sample.key}`: {len(sample.header)} columns; checked against {source}; " + (
            f"missing {missing}" if missing else "all present"
        )
        return ("fail" if missing else "pass"), detail, {"header": sample.header}

    async def cur_usage_types(self) -> tuple[Status, str, Any]:
        if self.export is None:
            return self._no_export()
        sample = await self._cur_sample()
        if sample is None:
            return "skip", "no CUR data file read", None
        from ai_cost_tracker.normalise import parse_meter

        bedrock = [r for r in sample.rows if _is_bedrock(r)]
        if not bedrock:
            scope = "first part of " if sample.truncated else ""
            return "skip", f"no Bedrock rows in {scope}`{sample.key}`", None
        usage = sorted({r.get("line_item_usage_type", "") for r in bedrock})
        unparsed = [u for u in usage if parse_meter("aws", u) is None]
        codes = dict(Counter(r.get("line_item_product_code", "?") for r in bedrock))
        kinds = dict(Counter(r.get("line_item_line_item_type", "?") for r in bedrock))
        regions = sorted({r.get("product_region_code", "") for r in bedrock})
        detail = (
            f"{len(bedrock)} Bedrock rows of {len(sample.rows)} read; {len(usage)} usage types, "
            f"{len(unparsed)} not parsed by normalise.parse_meter; product codes {codes}; "
            f"line item types {kinds}; regions {regions}"
        )
        names = sorted(
            {
                r.get("product_product_name", "")
                for r in bedrock
                if r.get("line_item_product_code") != "AmazonBedrock"
            }
            - {""}
        )
        sample_out = {
            "parsed": sorted(set(usage) - set(unparsed))[:40],
            "unparsed": unparsed[:60],
            "marketplace_product_names": names[:20],
        }
        return ("fail" if unparsed else "pass"), detail, sample_out

    async def pull_cost(self) -> tuple[Status, str, Any]:
        if self.export is None:
            return self._no_export()
        fn = _func(_optional("ai_cost_tracker.connectors.aws.billing"), "pull_cost")
        try:
            rows = await _call_module(
                fn, self.sessions, self.export, self.a, self.yesterday, self.yesterday
            )
        except LookupError as exc:
            return "skip", f"not available: {exc}", None
        meters = sorted({getattr(r, "sku_meter", "?") for r in rows})
        return "pass", f"{len(rows)} cost rows; usage types {meters[:10]}", None

    # ------------------------------------------------------------------ public price list

    async def _get_json(self, url: str) -> Any:
        resp = await self.http.get(url)
        resp.raise_for_status()
        return resp.json()

    async def offer_index(self) -> tuple[Status, str, Any]:
        # Assumption (spike-aws): Bedrock prices sit under offer code AmazonBedrock; other
        # Bedrock-related offers (e.g. Marketplace-sold models) show up here if they exist.
        data = await self._get_json(OFFER_INDEX)
        offers = sorted(k for k in (data.get("offers") or {}) if "bedrock" in k.lower())
        status: Status = "pass" if "AmazonBedrock" in offers else "fail"
        return status, f"Bedrock offer codes: {offers}", offers or None

    async def region_index(self) -> tuple[Status, str, Any]:
        data = await self._get_json(BEDROCK_REGION_INDEX)
        regions = data.get("regions") or {}
        missing = [g for g in self.regions if g not in regions]
        first = regions.get(self.regions[0]) or {}
        detail = (
            f"{len(regions)} regions; configured regions "
            + (f"missing {missing}" if missing else "all listed")
            + f"; entry fields {sorted(first)}"
        )
        status: Status = "pass" if regions and not missing else "fail"
        return status, detail, {self.regions[0]: first} if first else None

    async def price_usage_types(self) -> tuple[Status, str, Any]:
        notes = []
        if _optional("ai_cost_tracker.pricing.aws_prices") is None:
            notes.append("pricing.aws_prices not available")
        index = await self._get_json(BEDROCK_REGION_INDEX)
        entry = (index.get("regions") or {}).get(self.regions[0]) or {}
        path = entry.get("currentVersionUrl")
        if not path:
            return "fail", f"no currentVersionUrl for {self.regions[0]}", None
        offer = await self._get_json(path if path.startswith("http") else PRICE_LIST + path)
        products = offer.get("products") or {}
        usage = sorted({p.get("attributes", {}).get("usagetype", "") for p in products.values()})
        usage = [u for u in usage if u]
        from ai_cost_tracker.normalise import parse_meter

        unparsed = [u for u in usage if parse_meter("aws", u) is None]
        units = Counter(
            dim.get("unit", "?")
            for terms in (offer.get("terms", {}).get("OnDemand") or {}).values()
            for term in terms.values()
            for dim in (term.get("priceDimensions") or {}).values()
        )
        families = dict(Counter(p.get("productFamily", "?") for p in products.values()))
        notes.append(
            f"{len(products)} products, {len(usage)} usage types, {len(unparsed)} not parsed by "
            f"normalise.parse_meter; OnDemand units {dict(units)}; product families {families}"
        )
        sample = {
            "parsed": sorted(set(usage) - set(unparsed))[:30],
            "unparsed": unparsed[:60],
            "attribute_keys": sorted(
                {k for p in products.values() for k in p.get("attributes", {})}
            ),
        }
        return "pass", "; ".join(notes), sample


def _suffix(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return "." + name.split(".", 1)[1] if "." in name else "(none)"


def _period_key(key: str) -> tuple[str, str]:
    m = _PERIOD.search(key)
    return (m.group(1) if m else "", key)


async def _read_body(body: Any, limit: int) -> bytes:
    if body is None:
        return b""
    try:
        return await asyncio.to_thread(body.read, limit)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()


async def _gunzip_head(body: Any) -> tuple[str, bool]:
    """Decompress the start of a gzip CSV stream; drop a trailing partial line if cut short."""
    if body is None:
        return "", False
    decomp = zlib.decompressobj(wbits=31)
    chunks: list[bytes] = []
    size = 0
    read = 0
    ended = False
    try:
        while read < MAX_CUR_BYTES and size < MAX_CUR_TEXT:
            chunk = await asyncio.to_thread(body.read, 1024 * 1024)
            if not chunk:
                ended = True
                break
            read += len(chunk)
            out = decomp.decompress(chunk)
            chunks.append(out)
            size += len(out)
            if decomp.eof:
                ended = True
                break
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    text = b"".join(chunks).decode("utf-8", errors="replace")
    truncated = not (ended and decomp.eof)
    if truncated and "\n" in text:
        text = text[: text.rfind("\n") + 1]
    return text, truncated


async def run_spike(
    settings: Settings,
    account_id: str,
    out: Path,
    *,
    sessions: Any = None,
    http: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> Path:
    """Run every check against ``account_id`` and write the sanitised report to ``out``.

    ``sessions`` defaults to ``AwsSessions`` from ``settings`` and ``http`` (public price
    list) to a new client; both are closed here only when created here.
    """
    report = Report(account_id=account_id, started_at=now or datetime.now(UTC))
    owned_sessions = sessions is None
    if owned_sessions:
        try:
            from ai_cost_tracker.connectors.aws import sessions_from_settings

            sessions = sessions_from_settings(settings)
            report.checks.append(Check("AWS sessions", "pass", "AwsSessions created"))
        except (ImportError, NotImplementedError) as exc:
            sessions = None
            report.checks.append(
                Check("AWS sessions", "fail", f"not available: {type(exc).__name__} {exc}")
            )
    owned_http = http is None
    if owned_http:
        http = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
    try:
        await _Spike(settings, account_id, sessions, http, report).run()
    finally:
        if owned_http:
            await http.aclose()
        if owned_sessions and sessions is not None:
            try:
                await sessions.aclose()
            except Exception:  # closing must not lose the report
                pass
    await asyncio.to_thread(_write, out, render(report))
    return out


def _write(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def default_out(account_id: str, today: date | None = None) -> Path:
    """Suggested report path for the CLI: ``spike-aws-<account>-<date>.md``."""
    return Path(f"spike-aws-{account_id}-{(today or date.today()).isoformat()}.md")


__all__ = ["Check", "Report", "default_out", "render", "run_spike", "sanitise"]
