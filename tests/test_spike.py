"""``act spike-azure`` over httpx.MockTransport: success, differences, failures, sanitising."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from ai_cost_tracker import cli
from ai_cost_tracker.tools import spike
from ai_cost_tracker.tools.spike import Sanitiser, render_report, run_spike, trim

SUB = "11111111-2222-3333-4444-555555555555"
TENANT = "99999999-8888-7777-6666-555555555555"
RG = f"/subscriptions/{SUB}/resourceGroups/rg-secret-team/providers/Microsoft.CognitiveServices"
ACCOUNT = f"{RG}/accounts/aoai-payments-prod"
SPEECH = f"{RG}/accounts/speech-callcentre"
NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.c2lnbmF0dXJl"


async def no_sleep(_: float) -> None:
    return None


class FakeTokens:
    async def token(self) -> str:
        return TOKEN


class NoTokens:
    async def token(self) -> str:
        raise RuntimeError("DefaultAzureCredential failed to retrieve a token")


def error(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


class FakeAzure:
    """Just enough of ARM, Cost Management, Resource Graph, Monitor and Retail Prices."""

    def __init__(self, **options) -> None:
        self.payg = options.get("payg", True)
        self.three_dims_ok = options.get("three_dims_ok", False)
        self.camel_only = options.get("camel_only", False)
        self.long_range_ok = options.get("long_range_ok", False)
        self.next_link = options.get("next_link", True)
        self.metric_names = options.get(
            "metric_names", ["ProcessedPromptTokens", "GeneratedTokens", "AzureOpenAIRequests"]
        )
        self.sku = options.get("sku", "GlobalStandard")
        self.broken_deployments = options.get("broken_deployments", False)
        self.extra_price_service = options.get("extra_price_service", False)
        self.throttle_first = options.get("throttle_first", True)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.lower()
        if request.url.host == "prices.azure.com":
            assert "authorization" not in request.headers
            return self.prices(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if path.endswith("microsoft.costmanagement/query"):
            return self.cost(json.loads(request.content))
        if "microsoft.resourcegraph" in path:
            return self.graph(json.loads(request.content)["query"])
        if path.endswith("/deployments"):
            if self.broken_deployments:
                raise httpx.ConnectError("connection reset")
            return self.deployments(path)
        if path.endswith("metricdefinitions"):
            return self.metric_definitions()
        if path.endswith("microsoft.insights/metrics"):
            return self.metrics(request)
        return error(404, "NotFound", path)

    def cost(self, body: dict) -> httpx.Response:
        column = body["dataset"]["aggregation"]["totalCost"]["name"]
        headers = {"x-ms-ratelimit-microsoft.costmanagement-qpu-remaining": "QueryUsage=PT1M"}
        if self.throttle_first:
            self.throttle_first = False
            retry = {"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "1"}
            return httpx.Response(429, json={}, headers=retry)
        if column == "Cost" and self.payg:
            return error(400, "BadRequest", "Invalid query definition: column Cost is not valid")
        if len(body["dataset"]["grouping"]) > 2:
            return error(400, "BadRequest", "Grouping can have at most 2 dimensions")
        start = datetime.fromisoformat(body["timePeriod"]["from"])
        end = datetime.fromisoformat(body["timePeriod"]["to"])
        if (end - start).days > 366 and not self.long_range_ok:
            return error(400, "BadRequest", "The time period exceeds one year")
        values = body["dataset"]["filter"]["dimensions"]["values"]
        lower = values[0] == values[0].lower()
        rows = (
            []
            if (lower and self.camel_only)
            else [
                [1.25, 1000.0, ACCOUNT.lower(), "gpt-4o-mini-0718-Inp-glbl", 20261013, "USD"],
                [0.5, 200.0, ACCOUNT.lower(), "gpt-4o-mini-0718-Outp-glbl", 20261013, "USD"],
            ]
        )
        link = (
            f"https://management.azure.com/subscriptions/{SUB}/providers/Microsoft.CostManagement"
            "/query?api-version=2023-11-01&$skiptoken=AAAAsecretpage"
            if self.next_link
            else None
        )
        props = {
            "nextLink": link,
            "columns": [
                {"name": column, "type": "Number"},
                {"name": "UsageQuantity", "type": "Number"},
                {"name": "ResourceId", "type": "String"},
                {"name": "Meter", "type": "String"},
                {"name": "UsageDate", "type": "Number"},
                {"name": "Currency", "type": "String"},
            ],
            "rows": rows,
        }
        return httpx.Response(200, json={"properties": props}, headers=headers)

    def graph(self, query: str) -> httpx.Response:
        if "resourcecontainers" in query:
            data = [{"subscriptionId": SUB, "name": "Payments Production", "aiAccounts": 1}]
        elif "summarize count() by type, kind" in query:
            data = [
                {"type": "microsoft.cognitiveservices/accounts", "kind": "OpenAI", "count_": 1},
                {
                    "type": "microsoft.cognitiveservices/accounts",
                    "kind": "SpeechServices",
                    "count_": 1,
                },
            ]
        else:
            data = [
                {
                    "id": ACCOUNT,
                    "name": "aoai-payments-prod",
                    "kind": "OpenAI",
                    "location": "eastus2",
                    "sku": {"name": "S0"},
                    "tags": {"owner": "jane@x.com"},
                },
                {
                    "id": SPEECH,
                    "name": "speech-callcentre",
                    "kind": "SpeechServices",
                    "location": "eastus2",
                    "sku": {"name": "S0"},
                    "tags": {},
                },
            ]
        return httpx.Response(
            200, json={"totalRecords": len(data), "data": data, "tenantId": TENANT}
        )

    def deployments(self, path: str) -> httpx.Response:
        if "speech-callcentre" in path:
            return error(400, "OperationNotSupported", "Deployments are not supported")
        item = {
            "id": f"{ACCOUNT}/deployments/payments-chat",
            "name": "payments-chat",
            "sku": {"name": self.sku, "capacity": 50},
            "properties": {
                "model": {"format": "OpenAI", "name": "gpt-4o-mini", "version": "2024-07-18"}
            },
        }
        return httpx.Response(200, json={"value": [item]})

    def metric_definitions(self) -> httpx.Response:
        defs = [
            {
                "name": {"value": n, "localizedValue": n},
                "unit": "Count",
                "primaryAggregationType": "Total",
                "dimensions": [{"value": "ModelDeploymentName"}, {"value": "ModelName"}],
            }
            for n in self.metric_names
        ]
        defs.append({"name": {"value": "Latency"}, "unit": "Milliseconds", "dimensions": []})
        return httpx.Response(200, json={"value": defs})

    def metrics(self, request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        names = params["metricnames"][0].split(",")
        unknown = [n for n in names if n not in self.metric_names]
        if unknown:
            return error(400, "BadRequest", f"Failed to find metric configuration for {unknown}")
        assert params["top"] == ["1000"]
        assert params["$filter"] == ["ModelDeploymentName eq '*'"]
        value = [
            {
                "name": {"value": n},
                "timeseries": [
                    {
                        "metadatavalues": [
                            {"name": {"value": "modeldeploymentname"}, "value": "payments-chat"}
                        ],
                        "data": [{"timeStamp": "2026-10-14T08:00:00Z", "total": 42}],
                    }
                ],
            }
            for n in names
        ]
        return httpx.Response(
            200, json={"timespan": params["timespan"][0], "interval": "PT1H", "value": value}
        )

    def prices(self, request: httpx.Request) -> httpx.Response:
        flt = parse_qs(request.url.query.decode())["$filter"][0]
        items = [
            {
                "meterName": "gpt-4o-mini-0718-Inp-glbl",
                "unitOfMeasure": "1M",
                "serviceName": "Foundry Models",
                "productName": "Azure OpenAI",
                "retailPrice": 0.15,
            },
            {
                "meterName": "gpt-4o-mini-0718-Outp-glbl",
                "unitOfMeasure": "1M",
                "serviceName": "Foundry Models",
                "productName": "Azure OpenAI",
                "retailPrice": 0.6,
            },
        ]
        if self.extra_price_service and "serviceName" not in flt:
            items.append(
                {
                    "meterName": "gpt-4o-mini Inp Tokens",
                    "unitOfMeasure": "1K",
                    "serviceName": "Azure AI Studio",
                    "productName": "Models",
                }
            )
        return httpx.Response(200, json={"Items": items, "NextPageLink": None})


async def spike_run(fake: FakeAzure, tokens=None, **kwargs):
    return await run_spike(
        SUB,
        3,
        tokens=tokens or FakeTokens(),
        transport=httpx.MockTransport(fake),
        now=NOW,
        sleep=no_sleep,
        **kwargs,
    )


def statuses(report) -> dict[str, str]:
    return {k: f.status for k, f in report.findings.items()}


async def test_payg_subscription_confirms_assumptions():
    report, san = await spike_run(FakeAzure())
    st = statuses(report)
    for key in (
        "cost.column",
        "cost.columns",
        "cost.usage_date",
        "cost.currency",
        "cost.next_link",
        "cost.grouping",
        "cost.filter_case",
        "cost.range",
        "graph.subscriptions",
        "graph.kinds",
        "graph.location",
        "graph.deployment_type",
        "arm.deployments",
        "monitor.metric_names",
        "monitor.dimension",
        "monitor.span",
        "monitor.top",
        "monitor.alignment",
        "prices.filter",
        "prices.units",
        "prices.meters",
        "cost.throttle_headers",
    ):
        assert st[key] == "confirmed", (key, report.findings[key].observed)
    assert set(st) == {k for k, _, _ in spike.ASSUMPTIONS}
    assert [c.constant for c in report.constants] == ["DEFAULT_COST_COLUMN"]
    assert all(c.error is None for c in report.checks)
    assert (
        "other kinds ['SpeechServices'] have no deployments"
        in report.findings["graph.kinds"].observed
    )
    assert "qpu-retry-after" in report.findings["cost.throttle_headers"].observed


async def test_throttle_headers_unchecked_when_never_throttled():
    report, _ = await spike_run(FakeAzure(throttle_first=False))
    finding = report.findings["cost.throttle_headers"]
    assert finding.status == "unchecked"
    assert "qpu-remaining" in finding.observed


async def test_differences_become_constants_to_update():
    fake = FakeAzure(
        payg=False,
        three_dims_ok=False,
        camel_only=True,
        long_range_ok=True,
        next_link=False,
        metric_names=["InputTokens", "OutputTokens", "TotalTokens", "Requests"],
        sku="SomeNewTier",
        extra_price_service=True,
    )
    report, _ = await spike_run(fake)
    st = statuses(report)
    assert st["cost.column"] == "different"
    assert st["cost.filter_case"] == "different"
    assert st["cost.range"] == "different"
    assert st["cost.next_link"] == "unchecked"
    assert st["arm.deployments"] == "different"
    assert st["monitor.metric_names"] == "different"
    assert st["prices.filter"] == "different"
    # The metrics call falls back to the discovered token metrics, so span/top still get checked.
    assert st["monitor.span"] == "confirmed"
    assert st["monitor.dimension"] == "confirmed"  # checked on the token metrics that exist
    constants = {c.constant: c.observed for c in report.constants}
    assert "AI_RESOURCE_TYPES" in constants
    assert "SKU_DEPLOYMENT_TYPE" in constants
    assert constants["INPUT_METRIC"] == "['InputTokens']"
    assert constants["OUTPUT_METRIC"] == "['OutputTokens']"
    assert constants["REQUESTS_METRIC"] == "['Requests']"
    assert "PRICE_FILTER" in constants


async def test_failing_check_does_not_stop_the_others():
    report, _ = await spike_run(FakeAzure(broken_deployments=True))
    by_name = {c.name: c for c in report.checks}
    assert "ConnectError" in by_name["arm_deployments"].error
    assert report.findings["arm.deployments"].status == "unchecked"
    assert report.findings["arm.deployments"].observed.startswith("couldn't check: ConnectError")
    # Earlier and later checks still ran.
    assert report.findings["cost.column"].status == "confirmed"
    assert report.findings["prices.meters"].status == "confirmed"
    assert report.findings["cost.throttle_headers"].status == "confirmed"


async def test_no_credential_skips_arm_checks_but_not_retail():
    fake = FakeAzure()
    report, san = await spike_run(fake, tokens=NoTokens())
    assert "DefaultAzureCredential" in report.auth_error
    assert report.findings["cost.column"].observed.startswith("couldn't check: no Azure credential")
    assert report.findings["prices.units"].status == "confirmed"
    assert all(r.url.host == "prices.azure.com" for r in fake.requests)
    assert "Run `az login`" in render_report(report, san)


async def test_report_is_sanitised_and_complete():
    report, san = await spike_run(FakeAzure())
    md = render_report(report, san)
    for secret in (
        SUB,
        TENANT,
        "aoai-payments-prod",
        "rg-secret-team",
        "payments-chat",
        "Payments Production",
        "jane@x.com",
        "secretpage",
        TOKEN,
        "speech-callcentre",
    ):
        assert secret.lower() not in md.lower(), secret
    assert "{subscription-id}" in md
    assert "{account-1}" in md
    # Vocabulary that the report exists to show is kept.
    for kept in ("gpt-4o-mini", "GlobalStandard", "eastus2", "PreTaxCost", "ProcessedPromptTokens"):
        assert kept in md
    assert "## Constants to update" in md
    assert "| `connectors/azure/cost_query.py` | `DEFAULT_COST_COLUMN` |" in md
    assert md.count("| confirmed |") == len(report.findings)
    assert "x-ms-ratelimit-microsoft.costmanagement-qpu-remaining" in md


def test_sanitiser_replaces_ids_and_names():
    san = Sanitiser(SUB)
    san.learn({"id": ACCOUNT, "name": "aoai-payments-prod", "kind": "OpenAI", "tenantId": TENANT})
    san.learn({"subscriptionId": SUB, "name": "Payments Production"})
    san.learn({"properties": {"model": {"format": "OpenAI", "name": "gpt-4o"}}})
    san.learn({"id": f"{ACCOUNT}/deployments/gpt-4o"})
    text = san.text(
        f"{ACCOUNT.lower()}/deployments/gpt-4o in Payments Production for tenant {TENANT}, "
        f"other {'a' * 8}-0000-0000-0000-{'b' * 12}, Authorization: Bearer abc.def, {TOKEN}, "
        "next ...&$skiptoken=XYZ123&x=1, owner bob@example.com, kind OpenAI"
    )
    assert SUB not in text and TENANT not in text
    assert "/subscriptions/{subscription-id}/resourcegroups/{resource-group-1}" in text
    assert "/accounts/{account-1}/deployments/gpt-4o" in text  # model-named deployment kept
    assert "{subscription-name-1}" in text
    assert "{tenant-id}" in text and "{guid-1}" in text
    assert "Bearer {redacted-token}" in text and TOKEN not in text
    assert "$skiptoken={redacted}&x=1" in text
    assert "{email}" in text
    assert "kind OpenAI" in text


def test_sanitiser_payload_drops_tag_values_and_keeps_numbers():
    san = Sanitiser(SUB)
    out = san.payload({"id": ACCOUNT, "tags": {"owner": "jane"}, "rows": [[1.5, ACCOUNT]]})
    assert out["tags"] == {"owner": "{tag-value}"}
    assert out["rows"][0][0] == 1.5
    assert "aoai-payments-prod" not in json.dumps(out)


def test_trim_shortens_lists_and_strings():
    assert trim(list(range(20))) == list(range(20))  # a row of scalars is kept whole
    assert trim([[i] for i in range(5)], max_items=3) == [[0], [1], [2], "... 2 more"]
    assert trim({"a": "x" * 400})["a"].endswith("...")


def test_cli_spike_writes_report(tmp_path, monkeypatch, capsys):
    async def fake_run(subscription, days, **kwargs):
        assert (subscription, days) == (SUB, 5)
        return await spike_run(FakeAzure())

    monkeypatch.setattr(spike, "run_spike", fake_run)
    out = tmp_path / "spikes" / "report.md"
    assert cli.main(["spike-azure", "--subscription", SUB, "--days", "5", "--out", str(out)]) == 0
    assert out.read_text().startswith("# Azure pay-as-you-go spike")
    printed = capsys.readouterr().out
    assert "confirmed" in printed and SUB not in printed


@pytest.mark.parametrize(
    "argv", [["spike-azure"], ["spike-azure", "--subscription", SUB, "--days", "0"]]
)
def test_cli_spike_rejects_bad_input(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)
