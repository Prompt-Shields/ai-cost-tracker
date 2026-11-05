"""act spike-aws: every check runs against fake sessions and the report holds no secrets."""

import asyncio
import gzip
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ai_cost_tracker.config import Settings
from ai_cost_tracker.tools import spike_aws
from ai_cost_tracker.tools.spike_aws import run_spike, sanitise

FIXTURES = Path(__file__).parent / "fixtures" / "aws" / "spike"
ACCOUNT = "111122223333"
EXPORT_ACCOUNT = "999988887777"
BUCKET = "act-cur-bucket"
BASE = "ai-cost-tracker/ai-cost-tracker-cur2/"
SESSION = "act-session-42"
SESSION_TOKEN = "IQoJb3JpZ2luX2VjEXAMPLESESSIONTOKENVALUE0123456789"
ACCESS_KEY = "ASIAEXAMPLEKEY123456"
SECRET_KEY = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY12"
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
FM_ARN = "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
DATA_KEY = f"{BASE}data/BILLING_PERIOD=2026-11/ai-cost-tracker-cur2-00001.csv.gz"
MANIFEST_KEY = f"{BASE}metadata/BILLING_PERIOD=2026-11/ai-cost-tracker-cur2-Manifest.json"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeClient:
    """One boto3-like client; the sessions fake routes ``call`` to ``handle``."""

    def __init__(self, service: str, account: str, region: str) -> None:
        self.service, self.account, self.region = service, account, region
        # Present to prove the spike never reads credentials off a client.
        self.credentials = {"AccessKeyId": ACCESS_KEY, "SecretAccessKey": SECRET_KEY}

    def handle(self, method: str, kw: dict) -> dict:
        key = (self.service, method)
        if key == ("sts", "get_caller_identity"):
            return {
                "UserId": f"AROAEXAMPLEROLEID1234:{SESSION}",
                "Account": self.account,
                "Arn": f"arn:aws:sts::{self.account}:assumed-role/AiCostTrackerReadOnly/{SESSION}",
            }
        if key == ("bedrock", "list_foundation_models"):
            return {
                "modelSummaries": [
                    {"modelId": "anthropic.claude-3-7-sonnet-20250219-v1:0", "providerName": "A"},
                    {"modelId": "amazon.nova-pro-v1:0", "providerName": "Amazon"},
                ]
            }
        if key == ("bedrock", "list_inference_profiles"):
            if kw["typeEquals"] == "APPLICATION":
                return {
                    "inferenceProfileSummaries": [
                        {
                            "inferenceProfileId": "abc123profile",
                            "type": "APPLICATION",
                            "tags": [{"key": "owner", "value": "alice"}],
                        }
                    ]
                }
            if "nextToken" not in kw:
                return {
                    "inferenceProfileSummaries": [
                        {"inferenceProfileId": "us.anthropic.claude-3-7-sonnet-20250219-v1:0"}
                    ],
                    "nextToken": "bedrock-page-2",
                }
            return {
                "inferenceProfileSummaries": [{"inferenceProfileId": "us.amazon.nova-pro-v1:0"}]
            }
        if key == ("bedrock", "list_provisioned_model_throughputs"):
            if self.region == "us-west-2":
                raise RuntimeError(
                    f"AccessDenied with {ACCESS_KEY} token {SESSION_TOKEN} "
                    f"secret {SECRET_KEY} for owner@example.com"
                )
            return {
                "provisionedModelSummaries": [
                    {
                        "provisionedModelName": "nova-pt",
                        "foundationModelArn": FM_ARN,
                        "modelUnits": 2,
                        "status": "InService",
                    }
                ]
            }
        if key == ("cloudwatch", "list_metrics"):
            assert kw["Namespace"] == "AWS/Bedrock"
            if self.region == "us-west-2":
                return {"Metrics": []}
            return fixture("list_metrics_2.json" if kw.get("NextToken") else "list_metrics.json")
        if key == ("cloudwatch", "get_metric_data"):
            assert kw["StartTime"] == datetime(2026, 11, 3, tzinfo=UTC)
            dims = kw["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]
            assert dims[0]["Value"] == "amazon.nova-pro-v1:0"  # sorted, first
            return {
                "MetricDataResults": [
                    {
                        "Id": q["Id"],
                        "Timestamps": [datetime(2026, 11, 3, 1, tzinfo=UTC)],
                        "Values": [100.0],
                        "StatusCode": "Complete",
                    }
                    for q in kw["MetricDataQueries"]
                ]
            }
        if key == ("organizations", "list_accounts"):
            assert self.account == EXPORT_ACCOUNT and self.region == "us-east-1"
            return {
                "Accounts": [
                    {"Id": ACCOUNT, "Email": "owner@example.com", "Status": "ACTIVE"},
                    {"Id": EXPORT_ACCOUNT, "Email": "billing@example.com", "Status": "ACTIVE"},
                ]
            }
        if key == ("s3", "list_objects_v2"):
            assert self.account == EXPORT_ACCOUNT and kw["Bucket"] == BUCKET
            assert kw["Prefix"] == BASE
            if not kw.get("ContinuationToken"):
                return {"Contents": [{"Key": DATA_KEY}], "NextContinuationToken": "s3-page-2"}
            return {"Contents": [{"Key": MANIFEST_KEY}]}
        if key == ("s3", "get_object"):
            if kw["Key"] == MANIFEST_KEY:
                return {"Body": io.BytesIO((FIXTURES / "manifest.json").read_bytes())}
            assert kw["Key"] == DATA_KEY and kw["Range"].startswith("bytes=0-")
            return {"Body": io.BytesIO(gzip.compress((FIXTURES / "cur_sample.csv").read_bytes()))}
        raise AssertionError(f"unexpected call {key} {kw}")


class FakeSessions:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, str, str, str]] = []

    async def client(self, service: str, account_id: str, region: str) -> FakeClient:
        return FakeClient(service, account_id, region)

    async def call(self, client: FakeClient, method: str, **kwargs) -> dict:
        self.calls.append((client.service, client.account, client.region, method))
        return client.handle(method, kwargs)

    async def paginate(self, client, method, result_key, **kwargs):
        raise AssertionError("spike paginates itself")

    async def aclose(self) -> None:
        self.closed = True


def price_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/offers/v1.0/aws/index.json"):
        return httpx.Response(200, json=fixture("offer_index.json"))
    if url.endswith("/AmazonBedrock/current/region_index.json"):
        return httpx.Response(200, json=fixture("region_index.json"))
    if url.endswith("/20261101000000/us-east-1/index.json"):
        return httpx.Response(200, json=fixture("offer_us-east-1.json"))
    return httpx.Response(404)


def http_client(handler=price_handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def settings(**kwargs) -> Settings:
    base = {
        "environment": "test",
        "aws_regions": ["us-east-1", "us-west-2"],
        "aws_export_account_id": EXPORT_ACCOUNT,
        "aws_export_bucket": BUCKET,
    }
    return Settings(**(base | kwargs))


@pytest.fixture(autouse=True)
def stable_parser(monkeypatch):
    """Pin parse_meter so these tests check the spike, not W5's evolving usage-type rules."""
    import ai_cost_tracker.normalise as normalise

    def parse_meter(cloud: str, meter: str):
        assert cloud == "aws"
        known = meter.endswith(("-input-tokens", "-output-tokens", "-ModelUnits"))
        return SimpleNamespace(model="m") if known else None

    monkeypatch.setattr(normalise, "parse_meter", parse_meter)


def no_modules(monkeypatch):
    real = spike_aws._optional

    def optional(name: str):
        return None if name.startswith("ai_cost_tracker.connectors.aws.") else real(name)

    monkeypatch.setattr(spike_aws, "_optional", optional)


def checks(report: str) -> dict[str, str]:
    rows = [line.split(" | ") for line in report.splitlines() if line.startswith("| ")]
    return {r[1]: r[2] for r in rows[1:] if len(r) >= 4 and r[0] != "|---"}


async def run(tmp_path, sessions=None, **kwargs) -> str:
    async with http_client() as http:
        out = await run_spike(
            settings(**kwargs),
            ACCOUNT,
            tmp_path / "r" / "spike.md",
            sessions=sessions or FakeSessions(),
            http=http,
            now=NOW,
        )
    return out.read_text()


async def test_report_written_without_secrets(tmp_path, monkeypatch):
    no_modules(monkeypatch)
    sessions = FakeSessions()
    text = await run(tmp_path, sessions)
    status = checks(text)

    for name in (
        "Azure token + AssumeRoleWithWebIdentity",
        "STS GetCallerIdentity",
        "Bedrock ListFoundationModels (us-east-1)",
        "Bedrock ListInferenceProfiles (us-east-1)",
        "Bedrock ListProvisionedModelThroughputs (us-east-1)",
        "CloudWatch ListMetrics AWS/Bedrock (us-east-1)",
        "CloudWatch GetMetricData 1 day (us-east-1)",
        "Organizations ListAccounts (export account)",
        "S3 export prefix layout",
        "Export manifest (latest period)",
        "CUR data file header vs CUR_COLUMNS",
        "Price list: offer index lists Bedrock offers",
        "Price list: AmazonBedrock region_index.json",
        "Price list: usage types parsed (us-east-1)",
    ):
        assert status[name] == "pass", name
    assert status["Bedrock ListProvisionedModelThroughputs (us-west-2)"] == "fail"
    assert status["CloudWatch ListMetrics AWS/Bedrock (us-west-2)"] == "skip"
    assert status["CloudWatch GetMetricData 1 day (us-west-2)"] == "skip"
    # The made-up usage type in the CUR sample is reported as not parsed.
    assert status["CUR Bedrock usage types parsed"] == "fail"
    assert "USE1-MysteryModel-Units" in text
    assert "Claude 3.5 Sonnet (Amazon Bedrock Edition)" in text  # Marketplace row seen
    for name in (
        "inventory.check_access",
        "metrics.seen_model_ids (us-east-1)",
        "metrics.pull_usage (1 day)",
        "inventory.list_deployments",
        "inventory.list_accounts",
        "billing.pull_cost (1 day)",
    ):
        assert status[name] == "skip", name
    assert "not available" in text

    # Account ids and regions stay; credentials, tokens, sessions, emails, tags don't.
    assert ACCOUNT in text and EXPORT_ACCOUNT in text and "us-west-2" in text
    assert "assumed-role/AiCostTrackerReadOnly/{session}" in text
    for secret in (
        SESSION,
        SESSION_TOKEN,
        ACCESS_KEY,
        SECRET_KEY,
        "AROAEXAMPLEROLEID1234",
        "@example.com",
        "alice",
        "bedrock-page-2",
        "s3-page-2",
        "cw-page-2",
    ):
        assert secret not in text, secret
    assert not sessions.closed  # caller owns supplied sessions
    # Inference profiles and S3 listing followed their page tokens.
    assert sessions.calls.count(("s3", EXPORT_ACCOUNT, "us-east-1", "list_objects_v2")) == 2


async def test_real_module_functions_are_called_when_present(tmp_path, monkeypatch):
    seen: dict[str, tuple] = {}

    async def check_access(sessions, account_id):
        seen["check_access"] = (account_id,)
        return True

    async def seen_model_ids(sessions, account_id, region):
        return ["amazon.nova-pro-v1:0"]

    async def pull_usage(sessions, account_id, regions, deployments, start, end):
        seen["pull_usage"] = (account_id, tuple(regions), start, end)
        return [SimpleNamespace(model="nova-pro")]

    async def list_deployments(sessions, account_id, regions):
        seen["list_deployments"] = (account_id, tuple(regions))
        return [SimpleNamespace(name="amazon.nova-pro-v1:0", model="nova-pro", sku=None)]

    async def list_accounts(sessions, **kwargs):
        seen["list_accounts"] = kwargs
        return [SimpleNamespace(account_id=ACCOUNT, has_ai_resources=True)]

    async def pull_cost(sessions, export, account_id, start, end):
        seen["pull_cost"] = (export.bucket, account_id, start, end)
        raise NotImplementedError

    fakes = {
        "ai_cost_tracker.connectors.aws.inventory": SimpleNamespace(
            check_access=check_access,
            list_deployments=list_deployments,
            list_accounts=list_accounts,
        ),
        "ai_cost_tracker.connectors.aws.metrics": SimpleNamespace(
            pull_usage=pull_usage, seen_model_ids=seen_model_ids
        ),
        "ai_cost_tracker.connectors.aws.billing": SimpleNamespace(
            pull_cost=pull_cost, CUR_COLUMNS=("line_item_usage_type", "line_item_resource_id")
        ),
    }
    real = spike_aws._optional
    monkeypatch.setattr(spike_aws, "_optional", lambda name: fakes.get(name) or real(name))

    status = checks(await run(tmp_path))
    for name in (
        "inventory.check_access",
        "metrics.seen_model_ids (us-east-1)",
        "metrics.pull_usage (1 day)",
        "inventory.list_deployments",
        "inventory.list_accounts",
    ):
        assert status[name] == "pass", name
    assert status["billing.pull_cost (1 day)"] == "skip"  # stub raised NotImplementedError
    # billing.CUR_COLUMNS is used: line_item_resource_id isn't in the sample header.
    assert status["CUR data file header vs CUR_COLUMNS"] == "fail"
    assert seen["list_deployments"] == (ACCOUNT, ("us-east-1", "us-west-2"))
    assert seen["pull_usage"][2] == datetime(2026, 11, 3, tzinfo=UTC)
    assert seen["list_accounts"]["only"] == {ACCOUNT}
    assert seen["list_accounts"]["export_account_id"] == EXPORT_ACCOUNT
    assert seen["pull_cost"][:2] == (BUCKET, ACCOUNT)


async def test_export_checks_skipped_without_export(tmp_path, monkeypatch):
    no_modules(monkeypatch)
    status = checks(await run(tmp_path, aws_export_bucket="", aws_export_account_id=""))
    for name in (
        "Organizations ListAccounts (export account)",
        "S3 export prefix layout",
        "CUR data file header vs CUR_COLUMNS",
        "CUR Bedrock usage types parsed",
    ):
        assert status[name] == "skip", name
    assert status["STS GetCallerIdentity"] == "pass"


async def test_sessions_not_available_still_checks_price_list(tmp_path, monkeypatch):
    def broken(settings):
        raise NotImplementedError("AwsSessions stub")

    import ai_cost_tracker.connectors.aws as aws_pkg

    monkeypatch.setattr(aws_pkg, "sessions_from_settings", broken)
    async with http_client() as http:
        out = await run_spike(settings(), ACCOUNT, tmp_path / "s.md", http=http, now=NOW)
    status = checks(out.read_text())
    assert status["AWS sessions"] == "fail"
    assert status["STS GetCallerIdentity"] == "skip"
    assert status["Price list: AmazonBedrock region_index.json"] == "pass"


async def test_repeated_page_token_fails_the_check(tmp_path, monkeypatch):
    no_modules(monkeypatch)

    class Looping(FakeSessions):
        async def call(self, client, method, **kwargs):
            if method == "list_metrics":
                return {"Metrics": [], "NextToken": "same-token"}
            return await super().call(client, method, **kwargs)

    text = await run(tmp_path, Looping())
    status = checks(text)
    assert status["CloudWatch ListMetrics AWS/Bedrock (us-east-1)"] == "fail"
    assert "page token it already returned" in text
    assert status["S3 export prefix layout"] == "pass"


async def test_hanging_check_times_out(tmp_path, monkeypatch):
    no_modules(monkeypatch)
    monkeypatch.setattr(spike_aws, "CHECK_TIMEOUT", 0.05)

    class Slow(FakeSessions):
        async def call(self, client, method, **kwargs):
            if method == "get_caller_identity":
                await asyncio.sleep(10)
            return await super().call(client, method, **kwargs)

    status = checks(await run(tmp_path, Slow()))
    assert status["STS GetCallerIdentity"] == "fail"
    assert status["Price list: usage types parsed (us-east-1)"] == "pass"


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("jwt eyJhbGciOi.eyJzdWIiOi.sig", "eyJ"),
        (f"key {ACCESS_KEY}", ACCESS_KEY),
        (f"secret {SECRET_KEY}", SECRET_KEY),
        (f"token {SESSION_TOKEN}", SESSION_TOKEN),
        (f"arn:aws:sts::{ACCOUNT}:assumed-role/Role/{SESSION}", SESSION),
        (f"AROAEXAMPLEROLEID1234:{SESSION}", SESSION),
        ('"NextToken": "CiAKGjBpN"', "CiAKGjBpN"),
        ("owner owner@example.com", "owner@"),
    ],
)
def test_sanitise(raw, gone):
    assert gone not in sanitise(raw)


def test_sanitise_keeps_account_ids_and_regions():
    text = f"arn:aws:iam::{ACCOUNT}:role/AiCostTrackerReadOnly in us-east-1"
    assert sanitise(text) == text
