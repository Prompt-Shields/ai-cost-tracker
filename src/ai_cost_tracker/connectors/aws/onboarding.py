"""Connect AWS accounts without access keys (ARCHITECTURE.md §8, phase 4, issue #52).

Like GCP, the app can't grant itself access: someone signed in to each AWS account deploys
our CloudFormation template (``infra/aws/readonly-role.yaml``), which creates an IAM OIDC
provider trusting our Entra tenant and a read-only role that only the app's managed identity
may assume. Everything here builds what that person needs: the template text, a quick-create
link, an ``aws cloudformation deploy`` command and the permission list shown in the UI.

The export (management/payer) account deploys the same template with ``ExportAccount=true``,
which also creates the S3 bucket and the CUR 2.0 Data Export the billing pass reads.
"""

import logging
import shlex
from pathlib import Path
from urllib.parse import quote, urlencode

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.aws.ids import ACCOUNT_ID, role_arn

log = logging.getLogger(__name__)

# Repo copy of the template. Kept in infra/ next to the Bicep so it is reviewed as infra;
# the API image must ship it (Dockerfile: COPY infra/aws ./infra/aws).
TEMPLATE_PATH = Path(__file__).resolve().parents[4] / "infra" / "aws" / "readonly-role.yaml"
TEMPLATE_FILENAME = "ai-cost-tracker-readonly-role.yaml"
STACK_NAME = "ai-cost-tracker-readonly"
COLUMNS_PLACEHOLDER = "__CUR_COLUMNS__"

# Used only when connectors/aws/billing.py doesn't publish CUR_COLUMNS (yet): the columns
# the AWS model in ARCHITECTURE.md names. Assumption (spike-aws): CUR 2.0 column names as in
# AWS's Data Exports table dictionary.
FALLBACK_CUR_COLUMNS: tuple[str, ...] = (
    "line_item_usage_start_date",
    "line_item_usage_account_id",
    "line_item_line_item_type",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_usage_amount",
    "line_item_unblended_cost",
    "line_item_currency_code",
    "product_region_code",
    "product_servicecode",
    "product_product_name",
)

ACCOUNT_PLACEHOLDER = "<aws-account-id>"
TENANT_PLACEHOLDER = "<ACT_ENTRA_TENANT_ID>"
PRINCIPAL_PLACEHOLDER = "<ACT_AZURE_PRINCIPAL_ID>"
# Shown in fake (demo) mode so the onboarding screens have something to copy.
DEMO_EXPORT_ACCOUNT = "123456789012"
DEMO_EXPORT_BUCKET = "ai-cost-tracker-cur-123456789012"

# (permission, why). Shown in the UI and docs/aws-setup.md; the template grants exactly these.
PERMISSIONS: list[tuple[str, str]] = [
    (
        "sts:AssumeRoleWithWebIdentity (trust policy)",
        "Lets only the app's Azure managed identity (sub = ACT_AZURE_PRINCIPAL_ID, aud = the "
        "token audience) assume the role. No access keys exist.",
    ),
    (
        "cloudwatch:ListMetrics, cloudwatch:GetMetricData",
        "Reads Bedrock token and request counts per model from CloudWatch (AWS/Bedrock).",
    ),
    (
        "bedrock:ListFoundationModels, bedrock:ListInferenceProfiles, "
        "bedrock:GetInferenceProfile, bedrock:ListProvisionedModelThroughputs, "
        "bedrock:GetProvisionedModelThroughput, bedrock:ListTagsForResource",
        "Lists models, inference profiles and provisioned throughput with their tags "
        "(inventory and allocation). Never invokes a model.",
    ),
    (
        "organizations:ListAccounts, organizations:DescribeAccount (export account only)",
        "Names your accounts and suggests the ones that use Bedrock (discovery).",
    ),
    (
        "s3:ListBucket, s3:GetObject on the export bucket/prefix (export account only)",
        "Reads the CUR 2.0 Data Export (cost per usage type per day).",
    ),
]


def validate_account_id(account_id: str) -> str:
    """Return ``account_id`` if it is 12 digits, else ValueError (keeps input out of URLs)."""
    if not ACCOUNT_ID.match(account_id):
        raise ValueError(f"not an AWS account id: {account_id!r} (12 digits, no dashes)")
    return account_id


def cur_columns() -> tuple[str, ...]:
    """Columns the export selects: exactly what ``billing.py`` reads.

    Imported lazily so the template never drifts from the reader; the fallback keeps
    onboarding working while billing.py is incomplete (logged, so it's noticed).
    """
    try:
        from ai_cost_tracker.connectors.aws.billing import CUR_COLUMNS
    except ImportError:
        log.warning("connectors.aws.billing.CUR_COLUMNS missing; using the fallback columns")
        return FALLBACK_CUR_COLUMNS
    return tuple(CUR_COLUMNS)


def template_text() -> str:
    """The CloudFormation template with the export's column list filled in."""
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    return raw.replace(COLUMNS_PLACEHOLDER, ", ".join(cur_columns()))


def audience(settings: Settings) -> str:
    """The ``aud`` Entra puts in the managed identity token: the scope minus ``/.default``."""
    scope = settings.aws_azure_token_scope or "api://AzureADTokenExchange/.default"
    return scope.removesuffix("/.default")


def stack_parameters(settings: Settings, *, export_account: bool) -> dict[str, str]:
    """Stack parameter values from settings; unknown values are ``<...>`` placeholders."""
    params = {
        "RoleName": settings.aws_role_name,
        "EntraTenantId": settings.entra_tenant_id or TENANT_PLACEHOLDER,
        "AzurePrincipalId": settings.azure_principal_id or PRINCIPAL_PLACEHOLDER,
        "Audience": audience(settings),
    }
    if export_account:
        params["ExportAccount"] = "true"
        if settings.aws_export_bucket:
            params["ExportBucketName"] = settings.aws_export_bucket
        params["ExportPrefix"] = settings.aws_export_prefix.strip("/")
        params["ExportName"] = settings.aws_export_name
    return params


def _stack_region(settings: Settings) -> str:
    # Assumption (spike-aws): BCM Data Exports is served from us-east-1 only, so the export
    # stack goes there; IAM is global, so member stacks can use the same region.
    return settings.aws_export_region or "us-east-1"


def quick_create_url(settings: Settings, account_id: str, *, export_account: bool) -> str | None:
    """CloudFormation console "quick-create" link, or None without ``aws_cfn_template_url``.

    The console deploys into whichever account the person is signed in to, so ``account_id``
    is only validated here; the page tells them which account to sign in to.
    """
    validate_account_id(account_id)
    if not settings.aws_cfn_template_url:
        return None
    query = {"templateURL": settings.aws_cfn_template_url, "stackName": STACK_NAME}
    query |= {
        f"param_{k}": v
        for k, v in stack_parameters(settings, export_account=export_account).items()
        if not v.startswith("<")
    }
    region = _stack_region(settings)
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/create/review?{urlencode(query, quote_via=quote)}"
    )


def deploy_command(settings: Settings, *, export_account: bool) -> str:
    """One paste for AWS CloudShell in the target account.

    With a public template URL it downloads the template first; otherwise the person uploads
    the file from "Download template" (CloudShell: Actions > Upload file).
    """
    params = stack_parameters(settings, export_account=export_account)
    overrides = " \\\n    ".join(shlex.quote(f"{k}={v}") for k, v in params.items())
    deploy = (
        f"aws cloudformation deploy --template-file {TEMPLATE_FILENAME} \\\n"
        f"  --stack-name {STACK_NAME} --region {shlex.quote(_stack_region(settings))} \\\n"
        f"  --capabilities CAPABILITY_NAMED_IAM \\\n"
        f"  --parameter-overrides \\\n    {overrides}"
    )
    if settings.aws_cfn_template_url:
        url = shlex.quote(settings.aws_cfn_template_url)
        return f"curl -fsSL -o {TEMPLATE_FILENAME} {url} && \\\n{deploy}"
    return deploy


def is_export_account(settings: Settings, account_id: str) -> bool:
    return bool(settings.aws_export_account_id) and account_id == settings.aws_export_account_id


def account_role_arn(settings: Settings, account_id: str) -> str:
    return role_arn(account_id, settings.aws_role_name)


def missing_settings(settings: Settings) -> list[str]:
    """ACT_* settings still needed before accounts can be connected and billed; [] when done.

    Only ``ACT_AWS_ENABLED`` is needed to check access and list accounts; the tenant and
    principal go into the stack's trust policy, the export ones are needed for cost.
    """
    missing = []
    if not settings.aws_enabled:
        missing.append("ACT_AWS_ENABLED")
    if not settings.entra_tenant_id:
        missing.append("ACT_ENTRA_TENANT_ID")
    if not settings.azure_principal_id:
        missing.append("ACT_AZURE_PRINCIPAL_ID")
    if not settings.aws_export_account_id:
        missing.append("ACT_AWS_EXPORT_ACCOUNT_ID")
    if not settings.aws_export_bucket:
        missing.append("ACT_AWS_EXPORT_BUCKET")
    return missing
