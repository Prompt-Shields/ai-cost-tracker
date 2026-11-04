"""Connect GCP projects without a service-account key (ARCHITECTURE.md §8, issue #46).

Everything here builds text a human runs: a one-time Workload Identity Federation setup
script, per-project ``gcloud`` grants and the billing-export ``bq`` grant. The app never holds
Google admin rights, so it can't grant itself access the way "Connect Azure" does with the
admin's delegated token; instead it hands out commands and connects the project as soon as
the roles show up (``worker.discovery.check_pending_access``).

Command syntax follows Google's public gcloud / bq reference docs.
"""

import re
import shlex

from ai_cost_tracker.config import Settings

# (role, where it is granted, why the app needs it). Shown in the UI and docs/gcp-setup.md.
# Assumption (spike-gcp): roles/aiplatform.viewer and roles/monitoring.viewer both include
# resourcemanager.projects.get, which check_access and projects:search need; no extra
# "Browser" role is required.
ROLES: list[tuple[str, str, str]] = [
    (
        "roles/aiplatform.viewer",
        "each project",
        "Lists Vertex AI endpoints and deployed models (inventory) and lets the app see the "
        "project at all.",
    ),
    (
        "roles/monitoring.viewer",
        "each project",
        "Reads Vertex AI token and request counts from Cloud Monitoring (usage per model).",
    ),
    # Assumption (spike-gcp): serviceUsageViewer carries serviceusage.services.get. Viewer
    # rather than serviceUsageConsumer because we only read whether the Vertex AI API is
    # enabled; Consumer adds serviceusage.services.use, which we never need.
    (
        "roles/serviceusage.serviceUsageViewer",
        "each project",
        "Checks whether the Vertex AI API is enabled, so discovery only suggests projects that "
        "actually use it. Optional: without it the project still connects.",
    ),
    (
        "roles/bigquery.dataViewer",
        "billing export table",
        "Reads the detailed billing export (cost per SKU per day). Granted on the export table "
        "only; granting it on the dataset works too.",
    ),
    (
        "roles/bigquery.jobUser",
        "billing export project",
        "Runs the BigQuery query jobs that read the billing export.",
    ),
]
PROJECT_ROLES = [role for role, scope, _ in ROLES if scope == "each project"]

# Google's documented project id rule: 6-30 chars, lowercase letter first, no trailing hyphen.
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_TABLE = re.compile(r"^([\w-]+)\.([\w-]+)\.([\w-]+)$")
_SERVICE_ACCOUNT = re.compile(
    r"^([a-z][a-z0-9-]{4,28}[a-z0-9])@([a-z0-9-]+)\.iam\.gserviceaccount\.com$"
)
_AUDIENCE = re.compile(
    r"^//iam\.googleapis\.com/projects/\d+/locations/global/"
    r"workloadIdentityPools/([\w-]+)/providers/([\w-]+)$"
)

DEFAULT_POOL = "ai-cost-tracker"
DEFAULT_PROVIDER = "entra"
DEFAULT_SA_NAME = "ai-cost-tracker"
SERVICE_ACCOUNT_PLACEHOLDER = "<ACT_GCP_SERVICE_ACCOUNT>"
# Shown in fake (demo) mode so the onboarding screens have something to copy.
DEMO_SERVICE_ACCOUNT = "ai-cost-tracker@demo-gcp-project.iam.gserviceaccount.com"


def validate_project_id(project_id: str) -> str:
    """Return ``project_id`` if it is a valid GCP project id, else ValueError.

    Keeps user input out of the shell commands and REST paths we build.
    """
    if not PROJECT_ID.match(project_id):
        raise ValueError(
            f"not a GCP project id: {project_id!r} (6-30 lowercase letters, digits or hyphens, "
            "starting with a letter; use the project ID, not its name or number)"
        )
    return project_id


def missing_settings(settings: Settings) -> list[str]:
    """ACT_* settings the real GCP connector still needs; [] when it can run.

    The billing table is listed because cost (the point of the app) needs it, even though
    tokens and inventory work without it.
    """
    missing = []
    if not settings.gcp_enabled:
        missing.append("ACT_GCP_ENABLED")
    if not settings.gcp_wif_audience:
        missing.append("ACT_GCP_WIF_AUDIENCE")
    if not settings.gcp_service_account:
        missing.append("ACT_GCP_SERVICE_ACCOUNT")
    if not settings.gcp_billing_export_table:
        missing.append("ACT_GCP_BILLING_EXPORT_TABLE")
    return missing


def project_grant_commands(project_id: str, service_account: str) -> str:
    """``gcloud`` commands a project owner runs to let the app read one project."""
    validate_project_id(project_id)
    member = shlex.quote(f"serviceAccount:{service_account}")
    lines = [f"# Let the AI cost tracker read Vertex AI usage in {project_id} (read-only roles)."]
    for role in PROJECT_ROLES:
        lines.append(
            f"gcloud projects add-iam-policy-binding {project_id} \\\n"
            f"  --member={member} --role={role} --condition=None"
        )
    return "\n".join(lines)


def billing_grant_commands(table: str, service_account: str) -> str:
    """Commands that let the app query the billing export table (run once, not per project).

    ``table`` is ``<project>.<dataset>.<table>`` as in ACT_GCP_BILLING_EXPORT_TABLE.
    """
    match = _TABLE.match(table)
    if not match:
        raise ValueError(f"billing export table must be <project>.<dataset>.<table>: {table!r}")
    project, dataset, name = match.groups()
    member = shlex.quote(f"serviceAccount:{service_account}")
    # Assumption (spike-gcp): `bq add-iam-policy-binding` accepts a table as
    # project:dataset.table (table-level IAM, per the bq reference).
    return "\n".join(
        [
            "# Let the AI cost tracker read the billing export (run once).",
            f"bq add-iam-policy-binding --member={member} \\\n"
            f"  --role=roles/bigquery.dataViewer {shlex.quote(f'{project}:{dataset}.{name}')}",
            f"gcloud projects add-iam-policy-binding {shlex.quote(project)} \\\n"
            f"  --member={member} --role=roles/bigquery.jobUser --condition=None",
        ]
    )


def allowed_audience(settings: Settings) -> str:
    """The app id URI Entra puts in ``aud``: the token scope without ``/.default``."""
    scope = settings.gcp_azure_token_scope or "api://AzureADTokenExchange/.default"
    return scope.removesuffix("/.default")


def setup_script(settings: Settings) -> str:
    """One-time bash script (Cloud Shell) that creates the WIF pool, provider and SA.

    Pre-filled from settings where we know the values; anything unknown is a ``<...>``
    placeholder the script refuses to run with. Re-running it is safe: each resource is only
    created when missing.
    """
    host_project, sa_name = "<gcp-project-for-the-service-account>", DEFAULT_SA_NAME
    if sa := _SERVICE_ACCOUNT.match(settings.gcp_service_account or ""):
        sa_name, host_project = sa.groups()
    pool, provider = DEFAULT_POOL, DEFAULT_PROVIDER
    if aud := _AUDIENCE.match(settings.gcp_wif_audience or ""):
        pool, provider = aud.groups()
    tenant = settings.entra_tenant_id or "<entra-tenant-id>"
    principal = settings.azure_principal_id or "<ACT_AZURE_PRINCIPAL_ID>"
    billing = (
        billing_grant_commands(
            settings.gcp_billing_export_table, f"{sa_name}@{host_project}.iam.gserviceaccount.com"
        )
        if _TABLE.match(settings.gcp_billing_export_table or "")
        else "# Once ACT_GCP_BILLING_EXPORT_TABLE is set, the Connect page shows the two\n"
        "# commands that grant read access to the export table."
    )
    table = (
        settings.gcp_billing_export_table
        or "<project>.<dataset>.gcp_billing_export_resource_v1_<id>"
    )
    # Assumption (spike-gcp): a managed identity token for api://AzureADTokenExchange is an
    # Entra v2.0 token, so iss = https://login.microsoftonline.com/<tenant>/v2.0 (the issuer
    # Entra's own managed-identity federated credentials use) and aud = api://AzureADTokenExchange.
    # Google's Azure example uses https://sts.windows.net/<tenant>/ because its custom app
    # issues v1 tokens; if STS reports an issuer mismatch, switch --issuer-uri to that.
    # Assumption (spike-gcp): sub in that token is the managed identity's object id
    # (ACT_AZURE_PRINCIPAL_ID), so google.subject=assertion.sub names exactly our app.
    return f"""#!/usr/bin/env bash
# One-time Google Cloud setup for the AI cost tracker: no service-account keys anywhere.
# The app's Azure managed identity signs in to Google through Workload Identity Federation
# and acts as one read-only service account. Run in Cloud Shell as someone who can create
# workload identity pools and service accounts in PROJECT_ID (e.g. Owner of that project).
set -euo pipefail

PROJECT_ID={shlex.quote(host_project)}   # project that holds the pool and the service account
POOL_ID={shlex.quote(pool)}
PROVIDER_ID={shlex.quote(provider)}
SA_NAME={shlex.quote(sa_name)}
AZURE_TENANT_ID={shlex.quote(tenant)}          # Entra tenant (ACT_ENTRA_TENANT_ID)
AZURE_PRINCIPAL_ID={shlex.quote(principal)}    # app's managed identity object id
AUDIENCE={shlex.quote(allowed_audience(settings))}

for v in "$PROJECT_ID" "$AZURE_TENANT_ID" "$AZURE_PRINCIPAL_ID"; do
  case "$v" in "<"*) echo "Edit the placeholder $v at the top first." >&2; exit 1;; esac
done

gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \\
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com cloudbilling.googleapis.com \\
  --project "$PROJECT_ID"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SA_EMAIL="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

# 1. A pool with one OIDC provider that trusts our Entra tenant, and only our app's identity.
gcloud iam workload-identity-pools describe "$POOL_ID" --project "$PROJECT_ID" \\
  --location global >/dev/null 2>&1 ||
gcloud iam workload-identity-pools create "$POOL_ID" --project "$PROJECT_ID" \\
  --location global --display-name "AI cost tracker"
gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" --project "$PROJECT_ID" \\
  --location global --workload-identity-pool "$POOL_ID" >/dev/null 2>&1 ||
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \\
  --project "$PROJECT_ID" --location global --workload-identity-pool "$POOL_ID" \\
  --issuer-uri "https://login.microsoftonline.com/$AZURE_TENANT_ID/v2.0" \\
  --allowed-audiences "$AUDIENCE" \\
  --attribute-mapping "google.subject=assertion.sub" \\
  --attribute-condition "assertion.sub == '$AZURE_PRINCIPAL_ID'"

# 2. The service account the app acts as. It gets read-only roles per project later.
gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 ||
gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \\
  --display-name "AI cost tracker (read-only)"

# 3. Only the app's managed identity may act as that service account.
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" --project "$PROJECT_ID" \\
  --role roles/iam.workloadIdentityUser \\
  --member "principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/subject/$AZURE_PRINCIPAL_ID"

# 4. Billing export (detailed usage cost export to BigQuery must already be on).
{billing}

cat <<EOF

Done. Set these on the api and worker (no secrets: safe in plain config):
ACT_GCP_ENABLED=true
ACT_GCP_WIF_AUDIENCE=//iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/providers/$PROVIDER_ID
ACT_GCP_SERVICE_ACCOUNT=$SA_EMAIL
ACT_GCP_BILLING_EXPORT_TABLE={table}
EOF
"""
