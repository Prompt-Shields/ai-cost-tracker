"""Text builders for GCP onboarding: setup script, grant commands, missing settings."""

import shutil
import subprocess

import pytest

from ai_cost_tracker.config import Settings
from ai_cost_tracker.connectors.gcp import onboarding

SA = "act-reader@host-proj.iam.gserviceaccount.com"
TABLE = "bill-proj.billing_ds.gcp_billing_export_resource_v1_0123"


def _configured(**kw) -> Settings:
    values = {
        "gcp_enabled": True,
        "entra_tenant_id": "tenant-1",
        "azure_principal_id": "principal-1",
        "gcp_service_account": SA,
        "gcp_wif_audience": (
            "//iam.googleapis.com/projects/123456/locations/global/"
            "workloadIdentityPools/act-pool/providers/act-entra"
        ),
        "gcp_billing_export_table": TABLE,
    }
    return Settings(**{**values, **kw})


def test_roles_cover_every_api_we_call():
    roles = {role: scope for role, scope, why in onboarding.ROLES}
    assert roles == {
        "roles/aiplatform.viewer": "each project",
        "roles/monitoring.viewer": "each project",
        "roles/serviceusage.serviceUsageViewer": "each project",
        "roles/bigquery.dataViewer": "billing export table",
        "roles/bigquery.jobUser": "billing export project",
    }
    assert all(why for _, _, why in onboarding.ROLES)
    # Read-only only: nothing that can change a project.
    assert not any(r.endswith((".admin", ".editor", ".user")) for r in onboarding.PROJECT_ROLES)


@pytest.mark.parametrize("good", ["my-project", "abc123", "a" + "b" * 28 + "c"])
def test_valid_project_ids(good):
    assert onboarding.validate_project_id(good) == good


@pytest.mark.parametrize(
    "bad", ["short", "My-Project", "1project", "project-", "a" * 31, "p; rm -rf /", "123456789"]
)
def test_invalid_project_ids(bad):
    with pytest.raises(ValueError, match="project id"):
        onboarding.validate_project_id(bad)


def test_project_grant_commands():
    text = onboarding.project_grant_commands("my-project", SA)
    for role in onboarding.PROJECT_ROLES:
        assert (
            f"gcloud projects add-iam-policy-binding my-project \\\n"
            f"  --member=serviceAccount:{SA} --role={role} --condition=None"
        ) in text
    assert "bigquery" not in text
    with pytest.raises(ValueError):
        onboarding.project_grant_commands("Bad Project", SA)


def test_billing_grant_commands():
    text = onboarding.billing_grant_commands(TABLE, SA)
    assert (
        f"bq add-iam-policy-binding --member=serviceAccount:{SA} \\\n"
        "  --role=roles/bigquery.dataViewer "
        "bill-proj:billing_ds.gcp_billing_export_resource_v1_0123"
    ) in text
    assert "gcloud projects add-iam-policy-binding bill-proj" in text
    assert "--role=roles/bigquery.jobUser" in text
    with pytest.raises(ValueError, match="<project>.<dataset>.<table>"):
        onboarding.billing_grant_commands("dataset.table; drop", SA)


def test_missing_settings():
    assert onboarding.missing_settings(_configured()) == []
    assert onboarding.missing_settings(Settings()) == [
        "ACT_GCP_ENABLED",
        "ACT_GCP_WIF_AUDIENCE",
        "ACT_GCP_SERVICE_ACCOUNT",
        "ACT_GCP_BILLING_EXPORT_TABLE",
    ]
    assert onboarding.missing_settings(_configured(gcp_billing_export_table="")) == [
        "ACT_GCP_BILLING_EXPORT_TABLE"
    ]


def test_setup_script_prefilled_from_settings():
    script = onboarding.setup_script(_configured())
    assert "PROJECT_ID=host-proj" in script
    assert "SA_NAME=act-reader" in script
    assert "POOL_ID=act-pool" in script and "PROVIDER_ID=act-entra" in script
    assert "AZURE_TENANT_ID=tenant-1" in script
    assert "AZURE_PRINCIPAL_ID=principal-1" in script
    assert "AUDIENCE=api://AzureADTokenExchange" in script
    assert '--issuer-uri "https://login.microsoftonline.com/$AZURE_TENANT_ID/v2.0"' in script
    assert '--attribute-mapping "google.subject=assertion.sub"' in script
    assert '--allowed-audiences "$AUDIENCE"' in script
    assert "--role roles/iam.workloadIdentityUser" in script
    assert (
        "principal://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/"
        "workloadIdentityPools/$POOL_ID/subject/$AZURE_PRINCIPAL_ID"
    ) in script
    assert onboarding.billing_grant_commands(TABLE, SA) in script
    assert f"ACT_GCP_BILLING_EXPORT_TABLE={TABLE}" in script
    # No keys, ever.
    lowered = script.lower()
    assert "keys create" not in lowered and "private_key" not in lowered


def test_setup_script_defaults_and_placeholders():
    script = onboarding.setup_script(Settings())
    assert "POOL_ID=ai-cost-tracker" in script and "PROVIDER_ID=entra" in script
    assert "AZURE_TENANT_ID='<entra-tenant-id>'" in script
    assert "AZURE_PRINCIPAL_ID='<ACT_AZURE_PRINCIPAL_ID>'" in script
    assert "Edit the placeholder" in script
    assert "bq add-iam-policy-binding" not in script


def test_audience_follows_token_scope():
    settings = _configured(gcp_azure_token_scope="api://my-app/.default")
    assert onboarding.allowed_audience(settings) == "api://my-app"
    assert "AUDIENCE=api://my-app" in onboarding.setup_script(settings)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("settings", [Settings(), _configured()])
def test_setup_script_is_valid_bash(settings):
    result = subprocess.run(
        ["bash", "-n"], input=onboarding.setup_script(settings), text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
