"""AWS accounts and Bedrock inventory over a fake ``AwsSessions`` and boto3-doc-shaped fixtures."""

import json
from pathlib import Path
from typing import Any

import pytest

from ai_cost_tracker.connectors.aws import ids, inventory, metrics
from ai_cost_tracker.connectors.aws.auth import AwsAccessDenied
from ai_cost_tracker.normalise.aws_usage import canonical_model

FIXTURES = Path(__file__).parent.parent / "fixtures" / "aws" / "inventory"
MGMT = "111111111111"
ML = "123456789012"
WEB = "222222222222"
REGION = "us-east-1"
PROFILE_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123def456"
SONNET_PROFILE_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/zz9xy8"
PROVISIONED_ARN = "arn:aws:bedrock:us-east-1:123456789012:provisioned-model/pm0example01"
HAIKU = "anthropic.claude-3-5-haiku-20241022-v1:0"
SONNET_US = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: ``response["Error"]["Code"]``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakeSessions:
    """Stands in for ``AwsSessions``: canned responses keyed by (service, account, region, method).

    A value is a response dict, a list of pages (paginate), an exception to raise, or a
    callable taking the call's kwargs. ``denied`` lists (service, account, region) whose
    role can't be assumed.
    """

    def __init__(self, routes: dict[tuple, Any], denied: set[tuple] = frozenset()) -> None:
        self.routes = routes
        self.denied = denied
        self.calls: list[tuple] = []

    async def client(self, service: str, account_id: str, region: str) -> tuple:
        if (service, account_id, region) in self.denied or account_id in self.denied:
            raise AwsAccessDenied(f"cannot assume role in {account_id}")
        return (service, account_id, region)

    def _route(self, client: tuple, method: str, kwargs: dict) -> Any:
        self.calls.append((*client, method, kwargs))
        value = self.routes.get((*client, method))
        if value is None:
            raise AssertionError(f"unexpected call {client} {method} {kwargs}")
        if isinstance(value, Exception):
            raise value
        return value(kwargs) if callable(value) else value

    async def call(self, client: tuple, method: str, **kwargs: Any) -> dict:
        return self._route(client, method, kwargs)

    async def paginate(self, client: tuple, method: str, result_key: str, **kwargs: Any) -> list:
        value = self._route(client, method, kwargs)
        pages = value if isinstance(value, list) else [value]
        return [item for page in pages for item in page.get(result_key) or []]

    def methods(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[3] == method]


def fake_seen(table: dict[tuple[str, str], Any], calls: list | None = None):
    async def seen_model_ids(sessions, account_id, region, days=30):
        if calls is not None:
            calls.append((account_id, region, days))
        value = table.get((account_id, region), [])
        if isinstance(value, Exception):
            raise value
        return value

    return seen_model_ids


@pytest.fixture
def seen(monkeypatch):
    """Install a fake W3 ``metrics.seen_model_ids``; returns a setter."""

    def install(table: dict[tuple[str, str], Any], calls: list | None = None) -> None:
        monkeypatch.setattr(metrics, "seen_model_ids", fake_seen(table, calls), raising=False)

    return install


def org_routes(extra: dict | None = None) -> dict:
    return {
        ("organizations", MGMT, "us-east-1", "list_accounts"): [
            load("list_accounts_page1.json"),
            load("list_accounts_page2.json"),
        ],
        **(extra or {}),
    }


# --- list_accounts ---------------------------------------------------------------------------


async def test_list_accounts_default_returns_bedrock_users_only(seen):
    seen({(ML, REGION): [HAIKU]})
    sessions = FakeSessions(org_routes())
    accounts = await inventory.list_accounts(
        sessions, include_all=False, only=set(), export_account_id=MGMT, regions=[REGION]
    )
    assert [(a.cloud, a.account_id, a.name, a.has_ai_resources) for a in accounts] == [
        ("aws", ML, "ML Platform", True)
    ]


async def test_list_accounts_include_all_skips_closed_and_flags_each(seen):
    seen({(ML, "us-west-2"): [SONNET_US], (WEB, REGION): AwsAccessDenied("denied")})
    sessions = FakeSessions(org_routes())
    accounts = await inventory.list_accounts(
        sessions,
        include_all=True,
        only=set(),
        export_account_id=MGMT,
        regions=[" US-East-1 ", "us-west-2", "us-east-1"],
    )
    assert [(a.account_id, a.name, a.has_ai_resources) for a in accounts] == [
        (MGMT, "Management", False),
        (ML, "ML Platform", True),  # found in the second region
        (WEB, "Web Shop", False),  # metrics denied -> not flagged, not an error
    ]


async def test_list_accounts_only_lists_every_account_named_via_describe(seen):
    calls: list = []
    seen({(ML, REGION): [HAIKU]}, calls)

    def describe(kwargs: dict) -> dict:
        if kwargs["AccountId"] == ML:
            return load("describe_account.json")
        raise FakeClientError("AccountNotFoundException")

    sessions = FakeSessions(
        {("organizations", MGMT, "us-east-1", "describe_account"): describe},
        denied={"999999999999"},
    )
    accounts = await inventory.list_accounts(
        sessions,
        include_all=False,
        only={ML, "999999999999"},
        export_account_id=MGMT,
        regions=[REGION],
    )
    # The unreadable account stays listed so it can show "waiting for access".
    assert [(a.account_id, a.name, a.has_ai_resources) for a in accounts] == [
        (ML, "ML Platform", True),
        ("999999999999", "999999999999", False),
    ]
    assert sessions.methods("list_accounts") == []
    assert (ML, REGION, 30) in calls


async def test_list_accounts_only_without_organizations_access_uses_ids(seen):
    seen({})
    sessions = FakeSessions(
        {("organizations", MGMT, "us-east-1", "describe_account"): AwsAccessDenied("no org")}
    )
    accounts = await inventory.list_accounts(
        sessions, include_all=False, only={ML, WEB}, export_account_id=MGMT, regions=[REGION]
    )
    assert [(a.account_id, a.name) for a in accounts] == [(ML, ML), (WEB, WEB)]
    assert len(sessions.methods("describe_account")) == 1  # stops after the first denial


async def test_list_accounts_only_without_export_account(seen):
    seen({})
    accounts = await inventory.list_accounts(
        FakeSessions({}), include_all=False, only={ML}, export_account_id=None, regions=[REGION]
    )
    assert [(a.account_id, a.name) for a in accounts] == [(ML, ML)]


async def test_list_accounts_no_export_and_no_filter_is_empty(seen):
    seen({})
    assert (
        await inventory.list_accounts(
            FakeSessions({}), include_all=True, only=set(), export_account_id=None, regions=[]
        )
        == []
    )


@pytest.mark.parametrize(
    "error", [AwsAccessDenied("not mgmt"), FakeClientError("AWSOrganizationsNotInUseException")]
)
async def test_list_accounts_outside_an_organization_lists_export_account(seen, error):
    seen({(MGMT, REGION): [HAIKU]})
    sessions = FakeSessions({("organizations", MGMT, "us-east-1", "list_accounts"): error})
    accounts = await inventory.list_accounts(
        sessions, include_all=False, only=set(), export_account_id=MGMT, regions=[REGION]
    )
    assert [(a.account_id, a.has_ai_resources) for a in accounts] == [(MGMT, True)]


async def test_list_accounts_other_organizations_errors_raise(seen):
    seen({})
    sessions = FakeSessions(
        {("organizations", MGMT, "us-east-1", "list_accounts"): FakeClientError("Throttling")}
    )
    with pytest.raises(FakeClientError):
        await inventory.list_accounts(
            sessions, include_all=True, only=set(), export_account_id=MGMT, regions=[REGION]
        )


async def test_list_accounts_without_metrics_module_flags_nothing(monkeypatch):
    monkeypatch.delattr(metrics, "seen_model_ids", raising=False)
    accounts = await inventory.list_accounts(
        FakeSessions(org_routes()),
        include_all=True,
        only=set(),
        export_account_id=MGMT,
        regions=[REGION],
    )
    assert [a.has_ai_resources for a in accounts] == [False, False, False]


async def test_list_accounts_metrics_not_implemented_flags_nothing(seen):
    seen({(ML, REGION): NotImplementedError()})
    accounts = await inventory.list_accounts(
        FakeSessions(org_routes()),
        include_all=False,
        only=set(),
        export_account_id=MGMT,
        regions=[REGION],
    )
    assert accounts == []


# --- check_access ----------------------------------------------------------------------------


async def test_check_access_true_with_caller_identity():
    sessions = FakeSessions(
        {("sts", ML, "us-east-1", "get_caller_identity"): load("caller_identity.json")}
    )
    assert await inventory.check_access(sessions, ML) is True


async def test_check_access_false_when_role_cannot_be_assumed():
    assert await inventory.check_access(FakeSessions({}, denied={ML}), ML) is False


async def test_check_access_false_when_call_denied():
    sessions = FakeSessions({("sts", ML, "us-east-1", "get_caller_identity"): AwsAccessDenied("x")})
    assert await inventory.check_access(sessions, ML) is False


async def test_check_access_false_when_identity_is_another_account():
    sessions = FakeSessions(
        {("sts", WEB, "us-east-1", "get_caller_identity"): load("caller_identity.json")}
    )
    assert await inventory.check_access(sessions, WEB) is False


async def test_check_access_other_errors_raise():
    sessions = FakeSessions({("sts", ML, "us-east-1", "get_caller_identity"): RuntimeError("boom")})
    with pytest.raises(RuntimeError):
        await inventory.check_access(sessions, ML)


# --- list_deployments ------------------------------------------------------------------------


def bedrock_routes(extra: dict | None = None, region: str = REGION) -> dict:
    def tags(kwargs: dict) -> dict:
        return {
            PROFILE_ARN: load("tags_support_bot.json"),
            PROVISIONED_ARN: load("tags_nova_pro_prod.json"),
        }.get(kwargs["resourceARN"], load("tags_empty.json"))

    return {
        ("bedrock", ML, region, "list_inference_profiles"): load(
            "inference_profiles_us_east_1.json"
        ),
        ("bedrock", ML, region, "list_provisioned_model_throughputs"): load(
            "provisioned_us_east_1.json"
        ),
        ("bedrock", ML, region, "list_tags_for_resource"): tags,
        **(extra or {}),
    }


async def test_list_deployments_combines_seen_profiles_and_provisioned(seen):
    calls: list = []
    seen({(ML, REGION): [HAIKU, SONNET_US, PROFILE_ARN]}, calls)
    sessions = FakeSessions(bedrock_routes())
    deps = await inventory.list_deployments(sessions, ML, [REGION])
    rid = ids.resource_id(ML, REGION)
    assert all(d.cloud == "aws" and d.account_id == ML and d.resource_id == rid for d in deps)
    assert all(d.region == REGION for d in deps)
    got = {d.name: (d.model, d.sku, d.capacity, d.tags) for d in deps}
    assert got == {
        HAIKU: (canonical_model(HAIKU), None, None, {}),
        SONNET_US: (canonical_model(SONNET_US), None, None, {}),
        # The profile CloudWatch reported is named as CloudWatch named it.
        PROFILE_ARN: (
            canonical_model(HAIKU),
            "InferenceProfile",
            None,
            {"team": "support", "env": "prod"},
        ),
        SONNET_PROFILE_ARN: (canonical_model(SONNET_US), "InferenceProfile", None, {}),
        "nova-pro-prod": (
            canonical_model("amazon.nova-pro-v1:0"),
            "ProvisionedThroughput",
            2,
            {"team": "search"},
        ),
    }
    assert [d.name for d in deps] == sorted(d.name for d in deps)
    assert calls == [(ML, REGION, 30)]
    [profiles_call] = sessions.methods("list_inference_profiles")
    assert profiles_call[4] == {"typeEquals": "APPLICATION"}
    [pt_call] = sessions.methods("list_provisioned_model_throughputs")
    assert pt_call[4] == {"statusEquals": "InService"}


async def test_list_deployments_profile_seen_by_id_and_provisioned_by_arn(seen):
    seen({(ML, REGION): ["abc123def456", PROVISIONED_ARN]})
    deps = await inventory.list_deployments(FakeSessions(bedrock_routes()), ML, [REGION])
    by_sku = {(d.sku, d.name) for d in deps}
    assert by_sku == {
        ("InferenceProfile", "abc123def456"),
        ("InferenceProfile", SONNET_PROFILE_ARN),
        ("ProvisionedThroughput", PROVISIONED_ARN),
    }


async def test_list_deployments_skips_denied_and_unavailable_regions(seen):
    seen({(ML, REGION): [HAIKU], (ML, "eu-west-1"): AwsAccessDenied("x")})
    unavailable = inventory.EndpointConnectionError(
        endpoint_url="https://bedrock.ap-x-1.amazonaws.com"
    )
    sessions = FakeSessions(
        {
            ("bedrock", ML, REGION, "list_inference_profiles"): {"inferenceProfileSummaries": []},
            ("bedrock", ML, REGION, "list_provisioned_model_throughputs"): {
                "provisionedModelSummaries": []
            },
            ("bedrock", ML, "ap-x-1", "list_inference_profiles"): unavailable,
            ("bedrock", ML, "ap-x-1", "list_provisioned_model_throughputs"): unavailable,
        },
        denied={("bedrock", ML, "eu-west-1")},
    )
    deps = await inventory.list_deployments(sessions, ML, [REGION, "eu-west-1", "ap-x-1"])
    assert [(d.name, d.region) for d in deps] == [(HAIKU, REGION)]


async def test_list_deployments_bedrock_denied_still_reports_seen_models(seen):
    seen({(ML, REGION): [HAIKU]})
    sessions = FakeSessions({}, denied={("bedrock", ML, REGION)})
    deps = await inventory.list_deployments(sessions, ML, [REGION])
    assert [(d.name, d.sku) for d in deps] == [(HAIKU, None)]


async def test_list_deployments_tags_denied_means_no_tags(seen):
    seen({})
    sessions = FakeSessions(
        bedrock_routes(
            {("bedrock", ML, REGION, "list_tags_for_resource"): AwsAccessDenied("no tags")}
        )
    )
    deps = await inventory.list_deployments(sessions, ML, [REGION])
    assert len(deps) == 3 and all(d.tags == {} for d in deps)


async def test_list_deployments_without_metrics_lists_bedrock_resources(monkeypatch):
    monkeypatch.delattr(metrics, "seen_model_ids", raising=False)
    deps = await inventory.list_deployments(FakeSessions(bedrock_routes()), ML, [REGION])
    assert sorted(d.name for d in deps) == sorted(
        [PROFILE_ARN, SONNET_PROFILE_ARN, "nova-pro-prod"]
    )


async def test_list_deployments_other_errors_raise(seen):
    seen({})
    sessions = FakeSessions(
        bedrock_routes(
            {("bedrock", ML, REGION, "list_inference_profiles"): FakeClientError("Throttling")}
        )
    )
    with pytest.raises(FakeClientError):
        await inventory.list_deployments(sessions, ML, [REGION])
