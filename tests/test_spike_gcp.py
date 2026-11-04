"""act spike-gcp: every check runs against a fake client and the report holds no secrets."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ai_cost_tracker.config import Settings
from ai_cost_tracker.tools import spike_gcp
from ai_cost_tracker.tools.spike_gcp import run_spike, sanitise

FIXTURES = Path(__file__).parent / "fixtures" / "gcp" / "spike"
PROJECT = "demo-proj-123"
TOKEN = "ya29.super-secret-access-token"
TABLE = "billing-proj.billing_ds.gcp_billing_export_resource_v1_01A2B3_C4D5E6_F7A8B9"
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeTokens:
    async def token(self) -> str:
        return TOKEN


class FakeClient:
    """Routes by URL like the real APIs would; unknown URLs get a 404."""

    def __init__(self) -> None:
        self.tokens = FakeTokens()
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def get(self, url: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", url))
        if url.endswith(f"/v3/projects/{PROJECT}"):
            return {"projectId": PROJECT, "state": "ACTIVE", "displayName": "Demo"}
        if url.endswith("/services/aiplatform.googleapis.com"):
            return {"name": "projects/42/services/aiplatform.googleapis.com", "state": "ENABLED"}
        if url.endswith("/metricDescriptors"):
            return fixture("metric_descriptors.json")
        if url.endswith("/timeSeries"):
            assert params and params["interval.startTime"] == "2026-11-03T00:00:00Z"
            return fixture("time_series.json")
        if (
            url
            == f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/global/endpoints"
        ):
            return {}
        if url.startswith("https://us-central1-aiplatform.googleapis.com/"):
            return {
                "endpoints": [
                    {
                        "name": "projects/42/locations/us-central1/endpoints/99",
                        "labels": {"owner": "alice"},
                        "deployedModels": [{"id": "1", "model": "projects/42/models/7"}],
                    }
                ]
            }
        if url.endswith("/tables/gcp_billing_export_resource_v1_01A2B3_C4D5E6_F7A8B9"):
            return fixture("table.json")
        if url.endswith("/v1/services"):
            if params and params.get("pageToken"):
                return {"services": [{"serviceId": "C7E2-9256-1C43", "displayName": "Vertex AI"}]}
            return {
                "services": [{"serviceId": "6F81-5844-456A", "displayName": "Compute Engine"}],
                "nextPageToken": "page-2",
            }
        if url.endswith("/services/C7E2-9256-1C43/skus"):
            return fixture("catalog_skus.json")
        raise httpx.HTTPStatusError(
            f"404 from GET {url}",
            request=httpx.Request("GET", url),
            response=httpx.Response(404),
        )

    async def post(self, url: str, json: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append(("POST", url))
        assert url.endswith("/projects/billing-proj/queries")
        assert json and json["location"] == "EU" and json["useLegacySql"] is False
        assert "`billing-proj.billing_ds." in json["query"] and PROJECT not in json["query"]
        return {
            "jobComplete": True,
            "rows": [
                {"f": [{"v": "Vertex AI"}, {"v": "12"}, {"v": "3.5"}, {"v": "EUR"}]},
                {"f": [{"v": "Cloud Storage"}, {"v": "2"}, {"v": "0.1"}, {"v": "EUR"}]},
            ],
            "pageToken": "bq-page-token-xyz",
        }

    async def aclose(self) -> None:
        self.closed = True


def settings(**kwargs) -> Settings:
    base = {
        "environment": "test",
        "gcp_billing_export_table": TABLE,
        "gcp_billing_export_location": "EU",
        "gcp_regions": ["global", "us-central1"],
        "gcp_service_account": "act-reader@host-proj.iam.gserviceaccount.com",
    }
    return Settings(**(base | kwargs))


def no_modules(monkeypatch):
    real = spike_gcp._optional

    def optional(name: str):
        return None if name.startswith("ai_cost_tracker.connectors.gcp.") else real(name)

    monkeypatch.setattr(spike_gcp, "_optional", optional)


def checks(report: str) -> dict[str, str]:
    rows = [line.split(" | ") for line in report.splitlines() if line.startswith("| ")]
    return {r[1]: r[2] for r in rows[1:] if len(r) >= 4 and r[0] != "|---"}


async def test_report_written_without_secrets(tmp_path, monkeypatch):
    no_modules(monkeypatch)
    client = FakeClient()
    out = await run_spike(settings(), PROJECT, tmp_path / "r" / "spike.md", client=client, now=NOW)

    text = out.read_text()
    status = checks(text)
    assert status["WIF token exchange"] == "pass"
    assert status["Project get (Resource Manager v3)"] == "pass"
    assert status["Service Usage: aiplatform.googleapis.com state"] == "pass"
    assert (
        status["Metric descriptors aiplatform.googleapis.com/publisher/online_serving/*"] == "pass"
    )
    assert status["token_count time series (1 day): label keys"] == "pass"
    assert status["Vertex AI endpoints list (us-central1)"] == "pass"
    assert status["Billing export table schema (tables.get)"] == "pass"
    assert status["Billing export: services seen in 1 day"] == "pass"
    assert status["Cloud Billing catalog: Vertex AI SKUs"] == "pass"
    for name in (
        "inventory.check_access",
        "monitoring.pull_usage (1 day)",
        "billing.pull_cost (1 day)",
    ):
        assert status[name] == "skip"
    assert "not available" in text

    # Project ids stay; secrets, emails, billing account ids, label values, page tokens don't.
    assert PROJECT in text and "billing-proj" in text
    for secret in (TOKEN, "ya29", "01A2B3", "C4D5E6", "act-reader@", "alice", "page-token"):
        assert secret not in text, secret
    assert "Custom Training Nvidia T4 GPU" in text  # unparsed SKU sample listed
    assert not client.closed  # caller owns a supplied client


async def test_real_module_functions_are_called_when_present(tmp_path, monkeypatch):
    seen: dict[str, tuple] = {}

    async def check_access(client, project_id):
        seen["check_access"] = (project_id,)
        return True

    async def pull_usage(client, project_id, deployments, start, end):
        seen["pull_usage"] = (project_id, start, end)
        return [SimpleNamespace(model="gemini-2.5-flash")]

    async def list_deployments(client, project_id, regions):
        seen["list_deployments"] = (project_id, tuple(regions))
        return [SimpleNamespace(name="gemini-2.5-flash", model="gemini-2.5-flash")]

    async def pull_cost(client, **kwargs):
        seen["pull_cost"] = tuple(sorted(kwargs.items()))
        raise NotImplementedError

    fakes = {
        "ai_cost_tracker.connectors.gcp.inventory": SimpleNamespace(
            check_access=check_access, list_deployments=list_deployments
        ),
        "ai_cost_tracker.connectors.gcp.monitoring": SimpleNamespace(pull_usage=pull_usage),
        "ai_cost_tracker.connectors.gcp.billing": SimpleNamespace(pull_cost=pull_cost),
    }
    real = spike_gcp._optional
    monkeypatch.setattr(spike_gcp, "_optional", lambda name: fakes.get(name) or real(name))

    out = await run_spike(settings(), PROJECT, tmp_path / "spike.md", client=FakeClient(), now=NOW)
    status = checks(out.read_text())
    assert status["inventory.check_access"] == "pass"
    assert status["monitoring.pull_usage (1 day)"] == "pass"
    assert status["inventory.list_deployments"] == "pass"
    assert status["billing.pull_cost (1 day)"] == "skip"  # stub raised NotImplementedError
    assert seen["list_deployments"] == (PROJECT, ("global", "us-central1"))
    assert seen["pull_usage"][1] == datetime(2026, 11, 3, tzinfo=UTC)
    assert dict(seen["pull_cost"])["table"] == TABLE


async def test_failures_are_isolated_and_billing_skipped_without_table(tmp_path, monkeypatch):
    no_modules(monkeypatch)

    class Broken(FakeClient):
        async def get(self, url, params=None):
            if "serviceusage" in url:
                raise httpx.HTTPStatusError(
                    "403 from GET x: PERMISSION_DENIED",
                    request=httpx.Request("GET", url),
                    response=httpx.Response(403),
                )
            return await super().get(url, params)

    out = await run_spike(
        settings(gcp_billing_export_table=""), PROJECT, tmp_path / "s.md", client=Broken(), now=NOW
    )
    status = checks(out.read_text())
    assert status["Service Usage: aiplatform.googleapis.com state"] == "fail"
    assert status["Project get (Resource Manager v3)"] == "pass"
    assert status["Billing export table schema (tables.get)"] == "skip"
    assert status["Billing export: services seen in 1 day"] == "skip"


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
        ("token ya29.A0ARrdaM-xyz", "ya29"),
        ("jwt eyJhbGciOi.eyJzdWIiOi.sig", "eyJ"),
        ("billing 012345-6789AB-CDEF01", "6789AB"),
        ('"nextPageToken": "CiAKGjBpN"', "CiAKGjBpN"),
        ("sa x@y.iam.gserviceaccount.com", "x@y"),
    ],
)
def test_sanitise(raw, gone):
    assert gone not in sanitise(raw)


async def test_hanging_check_times_out(tmp_path, monkeypatch):
    no_modules(monkeypatch)
    monkeypatch.setattr(spike_gcp, "CHECK_TIMEOUT", 0.05)

    class Slow(FakeClient):
        async def get(self, url, params=None):
            if "serviceusage" in url:
                await asyncio.sleep(10)
            return await super().get(url, params)

    out = await run_spike(settings(), PROJECT, tmp_path / "s.md", client=Slow(), now=NOW)
    status = checks(out.read_text())
    assert status["Service Usage: aiplatform.googleapis.com state"] == "fail"
    assert status["Cloud Billing catalog: Vertex AI SKUs"] == "pass"
