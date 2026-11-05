"""AWS onboarding builders and the CloudFormation template (parsed, not deployed)."""

import shlex
import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.aws import onboarding

TENANT = "11111111-2222-3333-4444-555555555555"
PRINCIPAL = "66666666-7777-8888-9999-000000000000"


def _configured(**kw) -> Settings:
    values = {
        "aws_enabled": True,
        "entra_tenant_id": TENANT,
        "azure_principal_id": PRINCIPAL,
        "aws_export_account_id": "111122223333",
        "aws_export_bucket": "act-cur-bucket",
    }
    return Settings(**{**values, **kw})


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that accepts CloudFormation short-form tags (!Ref, !Sub, !If, ...)."""


def _cfn_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> dict:
    name = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {name: value}


_CfnLoader.add_multi_constructor("!", _cfn_tag)


def _template(text: str | None = None) -> dict:
    return yaml.load(text or onboarding.template_text(), Loader=_CfnLoader)  # noqa: S506


def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# --- template ---------------------------------------------------------------------------


def test_template_file_parses_and_has_the_contract_shape():
    raw = onboarding.TEMPLATE_PATH.read_text()
    assert onboarding.COLUMNS_PLACEHOLDER in raw  # repo copy: filled in when served
    t = _template(raw)
    assert t["AWSTemplateFormatVersion"] == "2010-09-09"
    assert set(t["Parameters"]) >= {
        "RoleName",
        "EntraTenantId",
        "AzurePrincipalId",
        "Audience",
        "IssuerVersion",
        "ExistingOidcProviderArn",
        "ExportAccount",
        "ExportBucketName",
        "ExportPrefix",
        "ExportName",
    }
    assert t["Parameters"]["RoleName"]["Default"] == Settings().aws_role_name
    assert t["Parameters"]["ExportPrefix"]["Default"] == Settings().aws_export_prefix
    assert t["Parameters"]["ExportName"]["Default"] == Settings().aws_export_name
    assert t["Parameters"]["Audience"]["Default"] == onboarding.audience(Settings())
    assert "IsExportAccount" in t["Conditions"]
    res = t["Resources"]
    for name in ("ExportBucket", "ExportBucketPolicy", "CostExport"):
        assert res[name]["Condition"] == "IsExportAccount"
    assert res["OidcProvider"]["Type"] == "AWS::IAM::OIDCProvider"
    assert res["CostExport"]["Type"] == "AWS::BCMDataExports::Export"
    assert "RoleArn" in t["Outputs"]


def test_every_parameter_the_app_passes_exists():
    params = set(_template()["Parameters"])
    for export in (False, True):
        assert set(onboarding.stack_parameters(_configured(), export_account=export)) <= params


def test_template_is_keyless_and_trusts_only_the_app_identity():
    text = onboarding.template_text()
    for word in ("AccessKey", "SecretAccessKey", "AWS::IAM::User", "ExternalId"):
        assert word not in text
    role = _template()["Resources"]["ReadOnlyRole"]["Properties"]
    trust_sub = role["AssumeRolePolicyDocument"]["Fn::Sub"]
    trust = trust_sub[0]
    assert '"Action": "sts:AssumeRoleWithWebIdentity"' in trust
    assert '"${IssuerKey}:aud": "${Audience}"' in trust
    assert '"${IssuerKey}:sub": "${AzurePrincipalId}"' in trust
    assert set(trust_sub[1]) == {"ProviderArn", "IssuerKey"}


def test_role_policy_is_read_only():
    role = _template()["Resources"]["ReadOnlyRole"]["Properties"]
    actions: list[str] = []
    for node in _walk(role["Policies"]):
        if isinstance(node, dict) and "Action" in node and "Effect" in node:
            assert node["Effect"] == "Allow"
            acts = node["Action"]
            actions += [acts] if isinstance(acts, str) else acts
    assert set(actions) >= {
        "cloudwatch:GetMetricData",
        "cloudwatch:ListMetrics",
        "bedrock:ListProvisionedModelThroughputs",
        "bedrock:ListInferenceProfiles",
        "bedrock:ListFoundationModels",
        "bedrock:GetInferenceProfile",
        "bedrock:ListTagsForResource",
        "organizations:ListAccounts",
        "organizations:DescribeAccount",
        "s3:GetObject",
        "s3:ListBucket",
    }
    verbs = {a.split(":", 1)[1] for a in actions}
    assert all(v.startswith(("List", "Get", "Describe")) for v in verbs), verbs
    assert not any("*" in a for a in actions)


def test_export_sql_selects_exactly_the_billing_columns():
    export = _template()["Resources"]["CostExport"]["Properties"]["Export"]
    sql = export["DataQuery"]["QueryStatement"]
    assert onboarding.COLUMNS_PLACEHOLDER not in sql
    cols = sql.removeprefix("SELECT ").removesuffix(" FROM COST_AND_USAGE_REPORT").split(", ")
    assert tuple(cols) == onboarding.cur_columns()
    out = export["DestinationConfigurations"]["S3Destination"]["S3OutputConfigurations"]
    assert out == {
        "OutputType": "CUSTOM",
        "Format": "TEXT_OR_CSV",
        "Compression": "GZIP",
        "Overwrite": "OVERWRITE_REPORT",
    }


def test_cur_columns_come_from_billing(monkeypatch):
    fake = types.ModuleType("ai_cost_tracker.connectors.aws.billing")
    fake.CUR_COLUMNS = ("a_col", "b_col")
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.connectors.aws.billing", fake)
    assert onboarding.cur_columns() == ("a_col", "b_col")
    assert "SELECT a_col, b_col FROM COST_AND_USAGE_REPORT" in onboarding.template_text()


def test_cur_columns_fallback_when_billing_has_none(monkeypatch):
    fake = types.ModuleType("ai_cost_tracker.connectors.aws.billing")
    monkeypatch.setitem(sys.modules, "ai_cost_tracker.connectors.aws.billing", fake)
    assert onboarding.cur_columns() == onboarding.FALLBACK_CUR_COLUMNS


# --- builders ---------------------------------------------------------------------------


@pytest.mark.parametrize("good", ["123456789012", "000000000001"])
def test_valid_account_ids(good):
    assert onboarding.validate_account_id(good) == good


@pytest.mark.parametrize("bad", ["12345678901", "1234567890123", "1234-5678-9012", "abc", ""])
def test_invalid_account_ids(bad):
    with pytest.raises(ValueError, match="AWS account id"):
        onboarding.validate_account_id(bad)


def test_stack_parameters_member_and_export():
    s = _configured(aws_azure_token_scope="api://custom-app/.default")
    assert onboarding.stack_parameters(s, export_account=False) == {
        "RoleName": "AiCostTrackerReadOnly",
        "EntraTenantId": TENANT,
        "AzurePrincipalId": PRINCIPAL,
        "Audience": "api://custom-app",
    }
    export = onboarding.stack_parameters(s, export_account=True)
    assert export["ExportAccount"] == "true"
    assert export["ExportBucketName"] == "act-cur-bucket"
    assert (export["ExportPrefix"], export["ExportName"]) == (
        "ai-cost-tracker",
        "ai-cost-tracker-cur2",
    )


def test_stack_parameters_placeholders_when_unset():
    params = onboarding.stack_parameters(Settings(), export_account=True)
    assert params["EntraTenantId"] == "<ACT_ENTRA_TENANT_ID>"
    assert params["AzurePrincipalId"] == "<ACT_AZURE_PRINCIPAL_ID>"
    assert "ExportBucketName" not in params  # template default: ai-cost-tracker-cur-<account>


def test_deploy_command():
    cmd = onboarding.deploy_command(_configured(), export_account=False)
    assert cmd.startswith("aws cloudformation deploy --template-file ")
    words = shlex.split(cmd.replace("\\\n", " "))
    assert words[words.index("--stack-name") + 1] == "ai-cost-tracker-readonly"
    assert words[words.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"
    assert words[words.index("--region") + 1] == "us-east-1"
    overrides = words[words.index("--parameter-overrides") + 1 :]
    assert f"AzurePrincipalId={PRINCIPAL}" in overrides
    assert not any(o.startswith("ExportAccount") for o in overrides)
    export = onboarding.deploy_command(_configured(), export_account=True)
    assert "ExportAccount=true" in export and "curl" not in export


def test_deploy_command_downloads_template_when_url_set():
    url = "https://bucket.s3.amazonaws.com/readonly-role.yaml"
    cmd = onboarding.deploy_command(_configured(aws_cfn_template_url=url), export_account=False)
    assert cmd.startswith(f"curl -fsSL -o {onboarding.TEMPLATE_FILENAME} {url} && ")


def test_deploy_command_quotes_hostile_values():
    cmd = onboarding.deploy_command(_configured(aws_role_name="x; rm -rf /"), export_account=False)
    assert "'RoleName=x; rm -rf /'" in cmd


def test_quick_create_url():
    assert onboarding.quick_create_url(_configured(), "123456789012", export_account=False) is None
    url = "https://bucket.s3.amazonaws.com/readonly-role.yaml"
    link = onboarding.quick_create_url(
        _configured(aws_cfn_template_url=url), "123456789012", export_account=True
    )
    parts = urlsplit(link)
    assert parts.netloc == "us-east-1.console.aws.amazon.com"
    route, _, query = parts.fragment.partition("?")
    assert route == "/stacks/create/review"
    q = {k: v[0] for k, v in parse_qs(query).items()}
    assert q["templateURL"] == url and q["stackName"] == "ai-cost-tracker-readonly"
    assert q["param_AzurePrincipalId"] == PRINCIPAL and q["param_ExportAccount"] == "true"
    with pytest.raises(ValueError):
        onboarding.quick_create_url(_configured(), "nope", export_account=False)


def test_quick_create_url_skips_placeholders():
    s = Settings(aws_cfn_template_url="https://x/t.yaml")
    link = onboarding.quick_create_url(s, "123456789012", export_account=False)
    assert "param_EntraTenantId" not in link and "param_RoleName" in link


def test_missing_settings():
    assert onboarding.missing_settings(_configured()) == []
    assert onboarding.missing_settings(Settings()) == [
        "ACT_AWS_ENABLED",
        "ACT_ENTRA_TENANT_ID",
        "ACT_AZURE_PRINCIPAL_ID",
        "ACT_AWS_EXPORT_ACCOUNT_ID",
        "ACT_AWS_EXPORT_BUCKET",
    ]


def test_permissions_listed_with_reasons():
    assert all(p and why for p, why in onboarding.PERMISSIONS)
    text = " ".join(p for p, _ in onboarding.PERMISSIONS)
    for action in ("cloudwatch:GetMetricData", "bedrock:ListInferenceProfiles", "s3:GetObject"):
        assert action in text
