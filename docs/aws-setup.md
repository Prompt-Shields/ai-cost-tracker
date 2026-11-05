# Connecting AWS (Amazon Bedrock)

The tracker reads Bedrock cost, token usage and inventory from AWS accounts **without access
keys**. The app's Azure managed identity gets an Entra token and swaps it for temporary AWS
credentials with `sts:AssumeRoleWithWebIdentity`, into a read-only role that each account
creates from one CloudFormation template. Nothing secret is stored anywhere: every setting
below is safe in plain config.

```
Azure managed identity --(Entra token, aud=api://AzureADTokenExchange)--> AWS STS
        AssumeRoleWithWebIdentity into AiCostTrackerReadOnly (trusts only that sub + aud)
                                   |
   CloudWatch (tokens) + Bedrock (inventory) in each account, CUR 2.0 export (cost)
```

There are two steps: **once**, deploy the stack in the export account (the one that holds the
cost export, normally the management/payer account); then **per account**, deploy the same
stack (one click with quick-create, or one command in AWS CloudShell) and add the account id
in the app.

In demo mode (`ACT_CONNECTOR=fake`) a demo AWS account is built in; none of this is needed.

## Prerequisites

- The app is deployed with a managed identity (`ACT_ENTRA_TENANT_ID` and
  `ACT_AZURE_PRINCIPAL_ID` set; the Bicep deployment sets the principal automatically).
- Someone who can create IAM roles and OIDC providers in each account (e.g.
  `AdministratorAccess` or `IAMFullAccess` + CloudFormation). In the export account they also
  need S3 and Billing and Cost Management Data Exports permissions.
- For cost across all accounts the export account should be the organization's management
  account (a CUR export there covers every linked account). Without AWS Organizations, each
  account is its own payer: use that account.

## 1. One-time: the export account stack

Open **Connect > AWS** in the app. While AWS isn't configured, it lists the missing settings,
a **Download template** link and the export-account command (the same values come from
`GET /api/v1/aws/setup`, `deploy_command_export`).

In the export account, open [AWS CloudShell](https://console.aws.amazon.com/cloudshell) in
**us-east-1**, upload the downloaded `ai-cost-tracker-readonly-role.yaml` (Actions > Upload
file) and paste the command. It looks like:

```bash
aws cloudformation deploy --template-file ai-cost-tracker-readonly-role.yaml \
  --stack-name ai-cost-tracker-readonly --region us-east-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    RoleName=AiCostTrackerReadOnly \
    EntraTenantId=<tenant id> \
    AzurePrincipalId=<managed identity object id> \
    Audience=api://AzureADTokenExchange \
    ExportAccount=true \
    ExportPrefix=ai-cost-tracker \
    ExportName=ai-cost-tracker-cur2
```

With `ExportAccount=true` the stack creates, besides the role:

- an S3 bucket `ai-cost-tracker-cur-<account id>` (or `ExportBucketName`), private, encrypted,
  retained if the stack is deleted, with the bucket policy AWS requires for Data Exports;
- a **CUR 2.0 Data Export** (CSV, GZIP, overwrite, daily granularity, refreshed by AWS as
  billing data changes) selecting exactly the columns the app reads.

Its `AppSettings` output prints the settings to add (CloudFormation console > Outputs, or
`aws cloudformation describe-stacks --stack-name ai-cost-tracker-readonly --region us-east-1
--query "Stacks[0].Outputs"`):

| Setting | Example | Notes |
|---|---|---|
| `ACT_AWS_ENABLED` | `true` | Turns the AWS connector on. |
| `ACT_AWS_EXPORT_ACCOUNT_ID` | `111122223333` | The account you just deployed in. |
| `ACT_AWS_EXPORT_BUCKET` | `ai-cost-tracker-cur-111122223333` | From the stack output. |
| `ACT_AWS_EXPORT_PREFIX` | `ai-cost-tracker` | Default. |
| `ACT_AWS_EXPORT_NAME` | `ai-cost-tracker-cur2` | Default. |
| `ACT_AWS_EXPORT_REGION` | `us-east-1` | Region of the bucket. Default. |
| `ACT_AWS_ACCOUNT_IDS` | `["123456789012"]` | Optional. Empty = accounts added in the UI plus discovery. |
| `ACT_AWS_REGIONS` | `["us-east-1","us-west-2"]` | Optional. Regions where you use Bedrock. |
| `ACT_AWS_ROLE_NAME` | `AiCostTrackerReadOnly` | Default; must match `RoleName` in every stack. |
| `ACT_AWS_AZURE_TOKEN_SCOPE` | `api://AzureADTokenExchange/.default` | Default; change only with a custom Entra app (and the stacks' `Audience`). |
| `ACT_AWS_CFN_TEMPLATE_URL` | `https://<bucket>.s3.amazonaws.com/ai-cost-tracker-readonly-role.yaml` | Optional, makes onboarding one click (below). |

On Azure Container Apps, set these as GitHub environment variables for the deploy workflow;
`infra/main.bicepparam` reads them and `infra/modules/apps.bicep` passes the non-empty ones to
the api, worker and migration job (see [deploy.md](deploy.md)). Locally, put them in `.env`;
`az login` stands in for the managed identity, so the token's `sub` is then your user's object
id, not the app's (deploy a test stack with your own object id as `AzurePrincipalId`).

Restart the api and worker. The Connect page now shows "Add an account".

**First cost data can take up to 24 hours** after the export is created.
Token usage and inventory work immediately.

### Optional: one-click quick-create

Upload the template downloaded from the app (not the repo copy, which has a placeholder for
the column list) to an S3 bucket readable by the people who will deploy it, and set
`ACT_AWS_CFN_TEMPLATE_URL` to its HTTPS URL. The app then shows a **Launch stack** button per
account (CloudFormation's quick-create page with every parameter pre-filled: the owner only
ticks "I acknowledge that AWS CloudFormation might create IAM resources with custom names" and
clicks *Create stack*), and the CloudShell command downloads the template by itself.

## 2. Adding accounts

**In the app:** Connect > AWS > *Add an account*, enter the 12-digit account id (dashes are
fine), *Add*.

- If the role is already there (the stack is deployed), the account is **connected** and a
  backfill starts.
- Otherwise it is **waiting for access** and the page shows the quick-create button (when
  configured) and the CloudShell command, with a link to share with the account owner
  (`/connect/aws/account/<id>`, backed by `GET /api/v1/aws/accounts/<id>/setup`). Once they
  deploy the stack, the account connects by itself within about 10 minutes (the worker
  re-checks waiting accounts), or immediately with *Check again*.

The member stack is the same template without `ExportAccount`: just the OIDC provider and the
read-only role. IAM is global, so the region doesn't matter.

Accounts the export account can see through AWS Organizations appear under *Visible accounts*
with a *Track* button; the worker's discovery adds those that use Bedrock as untracked and the
"new account found" banner offers to track them.

**API** (admin role for changes; each is written to the audit log):

| Call | Purpose |
|---|---|
| `GET /api/v1/aws/setup` | Configured?, missing settings, export location, permissions, deploy commands. |
| `GET /api/v1/aws/template.yaml` | The CloudFormation template (with the export's column list filled in). |
| `GET /api/v1/aws/accounts/visible` | Accounts visible through Organizations, with tracked/access state. |
| `POST /api/v1/aws/accounts` `{"account_id": "123456789012"}` | Add and check access (201). |
| `GET /api/v1/aws/accounts/{id}/setup` | Role ARN, quick-create link and command for the account owner. |
| `POST /api/v1/aws/accounts/{id}/check` | Re-check access now. |
| `POST /api/v1/connections/{id}/track?cloud=aws` | Track/untrack a discovered account. |

## What the stack grants

| Permission | Where | Why |
|---|---|---|
| `sts:AssumeRoleWithWebIdentity` (trust policy) | every account | Only the app's managed identity (`sub` = `ACT_AZURE_PRINCIPAL_ID`, `aud` = `api://AzureADTokenExchange`) may assume the role. No keys exist. |
| `cloudwatch:ListMetrics`, `cloudwatch:GetMetricData` | every account | Bedrock token and request counts per model (`AWS/Bedrock`). |
| `bedrock:ListFoundationModels`, `ListInferenceProfiles`, `GetInferenceProfile`, `ListProvisionedModelThroughputs`, `GetProvisionedModelThroughput`, `ListTagsForResource` | every account | Inventory: models, inference profiles, provisioned throughput and their tags. Never invokes a model. |
| `organizations:ListAccounts`, `organizations:DescribeAccount` | export account | Account names and discovery. |
| `s3:ListBucket`, `s3:GetObject` on the export bucket/prefix | export account | Reads the CUR 2.0 export. |

Everything is read-only; the app never changes anything in AWS.

**Why no External ID?** Issue #52 mentions one. An External ID protects a role trusted by
another *AWS account* against the confused-deputy problem. Here the role trusts a *web
identity* instead, and the trust policy pins the token's issuer (our Entra tenant), audience
and subject (the app's managed identity), which does the same job: no other caller can
present a matching token.

## Troubleshooting

Run `act spike-aws --account <id>` for a report that checks each step against a live account.

| Symptom | Likely cause and fix |
|---|---|
| `503 AWS is not enabled` | Set `ACT_AWS_ENABLED=true` and restart the api. |
| `InvalidIdentityToken` / "No OpenIDConnect provider found" | The provider URL must equal the token's `iss`. The template defaults to `https://login.microsoftonline.com/<tenant>/v2.0` (`IssuerVersion=v2`); if your tokens are v1, update the stack with `IssuerVersion=v1` (`https://sts.windows.net/<tenant>/`). Check with `az account get-access-token --scope api://AzureADTokenExchange/.default` and decode `iss`. |
| "Incorrect token audience" | The provider's client id list and the stack's `Audience` must equal the token's `aud` (`api://AzureADTokenExchange` for the default scope). |
| `AccessDenied` on `AssumeRoleWithWebIdentity` | `AzurePrincipalId` is not the identity the app runs as (locally it is your `az login` user), or the stack isn't deployed in that account / uses another `RoleName`. |
| Stack fails: provider "already exists" | IAM allows one OIDC provider per URL per account. Pass its ARN as `ExistingOidcProviderArn` and add `api://AzureADTokenExchange` to its audiences. |
| Account stays *waiting for access* | The stack isn't deployed (or failed) in that account. Check the stack's events, then *Check again*. |
| Tokens and inventory show, cost doesn't | Export not delivered yet (up to 24 h after creation), the export settings don't match the stack outputs, or the export account stack was deployed without `ExportAccount=true`. |
| Export creation fails | Data Exports must be created in us-east-1: deploy the export stack there. |
| No token usage for a region | Add the region to `ACT_AWS_REGIONS`; Bedrock metrics are per region. |
| Numbers differ from Cost Explorer by a day | Cost days are UTC (CUR is UTC). |

Details not yet checked on a live account (token issuer and `aud`, IAM condition keys, the
Data Export resource properties, bucket policy, CUR column names) are marked
`Assumption (spike-aws)` in `infra/aws/readonly-role.yaml` and
`src/ai_cost_tracker/connectors/aws/onboarding.py`; `act spike-aws` verifies them.
