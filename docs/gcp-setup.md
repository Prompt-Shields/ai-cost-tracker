# Connecting Google Cloud (Vertex AI)

The tracker reads Vertex AI cost, token usage and deployments from GCP projects **without a
service-account key**. The app's Azure managed identity signs in to Google through Workload
Identity Federation (WIF) and acts as one read-only service account. Nothing secret is stored
anywhere: every setting below is safe in plain config.

```
Azure managed identity --(Entra token)--> Google STS --(federated token)--> impersonate SA
                                                                              |
                     read-only roles on each project + the billing export  <--+
```

There are two parts: a **one-time setup** (about 10 minutes, done once per installation) and
**adding projects** (one form field per project, plus a command the project owner runs if the
app can't read it yet).

In demo mode (`ACT_CONNECTOR=fake`) a demo GCP project is built in; none of this is needed.

## 1. Prerequisite: billing export to BigQuery

Cost comes from Cloud Billing's **detailed usage cost** export to BigQuery. It must already be
on for the billing account that pays for your projects, because Google only exports data from
the day it was switched on (no history).

1. Cloud console > Billing > *Billing export* > *BigQuery export*.
2. Under **Detailed usage cost**, choose a project and dataset and save.
3. After a few hours a table named `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`
   appears. Its full name `<project>.<dataset>.<table>` is `ACT_GCP_BILLING_EXPORT_TABLE`;
   the dataset's location (e.g. `US`, `EU`) is `ACT_GCP_BILLING_EXPORT_LOCATION`.

Rows can land a day or two late, and cost days are UTC (the console defaults to Pacific time,
so its daily numbers can differ slightly near midnight).

## 2. One-time setup

Open **Connect > Google Cloud** in the app. While GCP isn't configured, it shows what is
missing and a ready-to-run setup script, pre-filled with your Entra tenant and the app's
managed identity. The same script comes from `GET /api/v1/gcp/setup` (`setup_script`).

Run it in [Cloud Shell](https://shell.cloud.google.com) as someone who can create workload
identity pools and service accounts in the project that will hold them (Owner of that project
is enough; it doesn't have to be a project you track). Edit `PROJECT_ID` at the top if it still
shows a placeholder. The script is safe to re-run; it:

1. enables the IAM, IAM Credentials, STS, Resource Manager, Service Usage and Cloud Billing
   APIs in that project;
2. creates a workload identity pool (`ai-cost-tracker`) with an OIDC provider (`entra`) that
   trusts your Entra tenant (`https://login.microsoftonline.com/<tenant>/v2.0`), accepts the
   audience `api://AzureADTokenExchange`, maps `google.subject=assertion.sub` and only admits
   the app's managed identity (`assertion.sub == '<ACT_AZURE_PRINCIPAL_ID>'`);
3. creates the service account `ai-cost-tracker@<project>.iam.gserviceaccount.com` (no roles
   yet, and **no keys**);
4. lets only the app's identity impersonate it: `roles/iam.workloadIdentityUser` for
   `principal://iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/subject/<azure_principal_id>`;
5. grants read access to the billing export, if `ACT_GCP_BILLING_EXPORT_TABLE` is already set.

It ends by printing the settings to add to the api and worker:

| Setting | Example | Notes |
|---|---|---|
| `ACT_GCP_ENABLED` | `true` | Turns the GCP connector on. |
| `ACT_GCP_WIF_AUDIENCE` | `//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/ai-cost-tracker/providers/entra` | The provider's full resource name. |
| `ACT_GCP_SERVICE_ACCOUNT` | `ai-cost-tracker@host-proj.iam.gserviceaccount.com` | The account the app acts as. |
| `ACT_GCP_BILLING_EXPORT_TABLE` | `billing-proj.billing.gcp_billing_export_resource_v1_0123AB_...` | From step 1. |
| `ACT_GCP_BILLING_EXPORT_LOCATION` | `US` | Default `US`. |
| `ACT_GCP_PROJECT_IDS` | `["ai-prod"]` | Optional. Empty = projects you add in the UI plus discovery. |
| `ACT_GCP_REGIONS` | `["global","us-central1"]` | Optional. Regions to list self-deployed endpoints in. |
| `ACT_GCP_AZURE_TOKEN_SCOPE` | `api://AzureADTokenExchange/.default` | Default; change only with a custom Entra app. |
| `ACT_REPORTING_CURRENCY` | `USD` | Totals across clouds are converted to this currency. |

On Azure Container Apps, set these as GitHub environment variables for the deploy workflow;
`infra/main.bicepparam` reads them and `infra/modules/apps.bicep` passes the non-empty ones to
the api, worker and migration job (see [deploy.md](deploy.md)). Locally, put them in `.env`;
`az login` stands in for the managed identity, so the federated subject is then your user's
object id, not the app's (add a second `workloadIdentityUser` binding for yourself if you want
to test locally).

Restart the api and worker. The Connect page now shows "Add a project".

## 3. Adding projects

**In the app:** Connect > Google Cloud > *Add a project*, enter the **project ID** (not its
name or number), *Add*.

- If the service account can already read it, the project is **connected** and a 90-day
  backfill starts (12 months of cost, as far as the export goes; 30 days of tokens).
- Otherwise it is **waiting for access** and the page shows the commands below, with a link
  to share with the project owner (`GET /api/v1/gcp/projects/<id>/grant`). Once they run them,
  the project connects by itself within about 10 minutes (the worker re-checks waiting projects), or
  immediately with *Check again*.

The commands a project owner runs (any role that can set IAM policy on the project, e.g.
Owner or Project IAM Admin):

```bash
SA=ai-cost-tracker@host-proj.iam.gserviceaccount.com
for role in roles/aiplatform.viewer roles/monitoring.viewer roles/serviceusage.serviceUsageViewer; do
  gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="serviceAccount:$SA" --role="$role" --condition=None
done
```

Projects the service account can already see appear under *Visible projects* with a *Track*
button. The worker's daily discovery also adds visible projects that use Vertex AI as
untracked, and the "new project found" banner offers to track them.

**API** (admin role for changes; each is written to the audit log):

| Call | Purpose |
|---|---|
| `GET /api/v1/gcp/setup` | Configured?, missing settings, roles, setup script, billing grant. |
| `GET /api/v1/gcp/projects/visible` | Projects the service account can see, with tracked/access state. |
| `POST /api/v1/gcp/projects` `{"project_id": "ai-prod"}` | Add and check access (201). |
| `GET /api/v1/gcp/projects/{id}/grant` | Commands to send the project owner. |
| `POST /api/v1/gcp/projects/{id}/check` | Re-check access now. |
| `POST /api/v1/connections/{id}/track?cloud=gcp` | Track/untrack a discovered project. |

## What each role is for

| Role | Granted on | Why |
|---|---|---|
| `roles/aiplatform.viewer` | each project | Lists Vertex AI endpoints and deployed models (inventory) and lets the app see the project. |
| `roles/monitoring.viewer` | each project | Reads Vertex AI token and request counts from Cloud Monitoring. |
| `roles/serviceusage.serviceUsageViewer` | each project | Checks whether the Vertex AI API is enabled, so discovery only suggests projects that use it. Optional. |
| `roles/bigquery.dataViewer` | billing export table | Reads the detailed billing export. Dataset-level works too. |
| `roles/bigquery.jobUser` | billing export project | Runs the query jobs that read the export. |
| `roles/iam.workloadIdentityUser` | the service account | Lets only the app's managed identity act as the service account. |

All are read-only. The app never needs to change anything in GCP. The billing grant (run once,
also shown on the Connect page and in the setup script):

```bash
bq add-iam-policy-binding --member=serviceAccount:$SA \
  --role=roles/bigquery.dataViewer BILLING_PROJECT:DATASET.TABLE
gcloud projects add-iam-policy-binding BILLING_PROJECT \
  --member=serviceAccount:$SA --role=roles/bigquery.jobUser --condition=None
```

## Troubleshooting

Run `act spike-gcp --project <id>` for a report that checks each step against a live project.

| Symptom | Likely cause and fix |
|---|---|
| `503 GCP is not enabled` / `not configured yet` | Set the settings listed in the message and restart the api. |
| STS error `invalid_grant` mentioning the issuer | The provider's issuer must match the token's `iss`. The script uses `https://login.microsoftonline.com/<tenant>/v2.0`; if your tokens are v1, recreate the provider with `--issuer-uri https://sts.windows.net/<tenant>/` (decode a token with `az account get-access-token --scope api://AzureADTokenExchange/.default` and check `iss`). |
| STS error about the audience | `--allowed-audiences` must equal the token's `aud` (`api://AzureADTokenExchange` for the default scope). |
| STS error about the attribute condition | `ACT_AZURE_PRINCIPAL_ID` is not the identity the app runs as (locally it is your `az login` user). |
| `generateAccessToken` 403 `iam.serviceAccounts.getAccessToken` | The `workloadIdentityUser` binding (setup step 4) is missing or names another subject; or the IAM Credentials API is not enabled in the service account's project. |
| Project stays *waiting for access* | The owner hasn't run the grant commands, or ran them for another service account. IAM changes can take a few minutes; use *Check again*. |
| Tokens and inventory show, cost doesn't | Billing export prerequisite: check `ACT_GCP_BILLING_EXPORT_TABLE`, the `dataViewer`/`jobUser` grants, and that the export has started (no history before it was switched on). |
| No token usage for a project | Cloud Monitoring API disabled in that project: `gcloud services enable monitoring.googleapis.com --project PROJECT_ID`. |
| Numbers differ from the console by a day | Cost days are UTC; the console uses Pacific time by default. |

Assumptions in this guide that haven't yet been checked on a live project (token issuer and
`sub` claim, exact role contents) are marked `Assumption (spike-gcp)` in
`src/ai_cost_tracker/connectors/gcp/onboarding.py`; `act spike-gcp` verifies them.
