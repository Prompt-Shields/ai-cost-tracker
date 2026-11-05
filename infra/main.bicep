// ai-cost-tracker on Azure: Container Apps (api, worker, web, migration job), Postgres
// Flexible Server with TimescaleDB, Azure Cache for Redis, Key Vault, ACR and one
// user-assigned managed identity. Deployed per resource group by .github/workflows/deploy.yml;
// see docs/deploy.md. Unverified against a live subscription (issue #2).

targetScope = 'resourceGroup'

@description('Short lowercase prefix for resource names, e.g. "act" or "act-dev". Letters, digits, hyphens.')
@minLength(2)
@maxLength(12)
param namePrefix string

param location string = resourceGroup().location

@description('Entra directory (tenant) ID of the sign-in app registration (docs/entra-setup.md).')
param entraTenantId string

@description('Entra application (client) ID of the sign-in app registration.')
param entraClientId string

@description('Entra group object IDs allowed to sign in. Empty: anyone in the tenant.')
param allowedGroupIds array = []

@description('Entra group object IDs per app role, e.g. {"admin": ["<group-id>"]}.')
param roleGroups object = {}

@description('Image tag (commit SHA) the api, worker and web apps run.')
param imageTag string

@description('Image tag the migration job runs. Defaults to imageTag.')
param migrationImageTag string = imageTag

@description('false on the bootstrap pass: creates everything except the apps, so images can be pushed first.')
param deployApps bool = true

@description('false on the bootstrap pass, before the first image exists.')
param deployMigrationJob bool = true

@description('Set only when creating the server or rotating; empty keeps the existing password and secrets.')
@secure()
param postgresAdminPassword string = ''

@allowed(['azure', 'fake'])
param connector string = 'azure'

@description('Subscriptions to track. Empty: every subscription the identity can read that has AI resources.')
param azureSubscriptionIds array = []

// GCP (Vertex AI) connection through Workload Identity Federation: no keys, so nothing here is
// secret. Empty values are not set on the containers, so config.py defaults apply.
// docs/gcp-setup.md explains each one.
@description('Turn on the GCP connector (ACT_GCP_ENABLED).')
param gcpEnabled bool = false

@description('GCP project ids to track. Empty: every visible project that uses Vertex AI.')
param gcpProjectIds array = []

@description('WIF provider resource name, //iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>.')
param gcpWifAudience string = ''

@description('Email of the read-only service account the app impersonates.')
param gcpServiceAccount string = ''

@description('Entra token scope exchanged with Google STS. Empty: api://AzureADTokenExchange/.default.')
param gcpAzureTokenScope string = ''

@description('Detailed billing export table, <project>.<dataset>.<table>.')
param gcpBillingExportTable string = ''

@description('BigQuery location of the billing export dataset. Empty: US.')
param gcpBillingExportLocation string = ''

@description('Vertex AI regions to list endpoints in. Empty: the config.py default list.')
param gcpRegions array = []

// AWS (Bedrock) connection: the managed identity assumes a read-only role in each account via
// Entra OIDC (no access keys), so nothing here is secret either. Empty values are not set on
// the containers, so config.py defaults apply. docs/aws-setup.md explains each one.
@description('Turn on the AWS connector (ACT_AWS_ENABLED).')
param awsEnabled bool = false

@description('AWS account ids to track. Empty: accounts added in the UI plus discovery.')
param awsAccountIds array = []

@description('Read-only role the CloudFormation stack creates in each account. Empty: AiCostTrackerReadOnly.')
param awsRoleName string = ''

@description('Entra token scope exchanged with AWS STS. Empty: api://AzureADTokenExchange/.default.')
param awsAzureTokenScope string = ''

@description('Bedrock regions to read. Empty: the config.py default list.')
param awsRegions array = []

@description('Account holding the CUR 2.0 Data Export (normally the management account).')
param awsExportAccountId string = ''

@description('S3 bucket of the CUR 2.0 Data Export.')
param awsExportBucket string = ''

@description('S3 prefix of the export. Empty: ai-cost-tracker.')
param awsExportPrefix string = ''

@description('Data Export name. Empty: ai-cost-tracker-cur2.')
param awsExportName string = ''

@description('Region of the export bucket. Empty: us-east-1.')
param awsExportRegion string = ''

@description('Public S3 URL of infra/aws/readonly-role.yaml for one-click quick-create links.')
param awsCfnTemplateUrl string = ''

@description('Currency every total is reported in. Empty: USD.')
param reportingCurrency string = ''

@description('Public web URL if a custom domain is bound; empty uses the Container Apps FQDN.')
param publicWebUrl string = ''

@description('CIDR ranges allowed to reach the api and web. Empty: reachable from anywhere, behind Entra sign-in.')
param allowedIpRanges array = []

param postgresSkuName string = 'Standard_B1ms'
param postgresSkuTier string = 'Burstable'
param postgresStorageGB int = 32

param apiMinReplicas int = 1
param apiMaxReplicas int = 3
param webMinReplicas int = 1

var tags = {
  app: 'ai-cost-tracker'
  'managed-by': 'bicep'
}
var suffix = uniqueString(resourceGroup().id)
var compactPrefix = toLower(replace(namePrefix, '-', ''))

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    name: '${namePrefix}-id'
    location: location
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    name: take('${compactPrefix}acr${suffix}', 50)
    location: location
    tags: tags
    pullPrincipalId: identity.outputs.principalId
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    // Vault names are global and at most 24 characters.
    name: take('${take(compactPrefix, 8)}kv${suffix}', 24)
    location: location
    tags: tags
    readerPrincipalId: identity.outputs.principalId
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    name: '${namePrefix}-pg-${suffix}'
    location: location
    tags: union(tags, { 'act-component': 'postgres' })
    keyVaultName: keyVault.outputs.name
    administratorPassword: postgresAdminPassword
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGB: postgresStorageGB
  }
}

module redis 'modules/redis.bicep' = {
  name: 'redis'
  params: {
    name: '${namePrefix}-redis-${suffix}'
    location: location
    tags: tags
    keyVaultName: keyVault.outputs.name
  }
}

module apps 'modules/apps.bicep' = {
  name: 'apps'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    environmentId: monitoring.outputs.environmentId
    defaultDomain: monitoring.outputs.defaultDomain
    registryServer: registry.outputs.loginServer
    keyVaultUri: keyVault.outputs.uri
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    identityPrincipalId: identity.outputs.principalId
    deployApps: deployApps
    deployMigrationJob: deployMigrationJob
    appImageTag: imageTag
    migrationImageTag: migrationImageTag
    entraTenantId: entraTenantId
    entraClientId: entraClientId
    allowedGroupIds: allowedGroupIds
    roleGroups: roleGroups
    connector: connector
    azureSubscriptionIds: azureSubscriptionIds
    gcpEnabled: gcpEnabled
    gcpProjectIds: gcpProjectIds
    gcpWifAudience: gcpWifAudience
    gcpServiceAccount: gcpServiceAccount
    gcpAzureTokenScope: gcpAzureTokenScope
    gcpBillingExportTable: gcpBillingExportTable
    gcpBillingExportLocation: gcpBillingExportLocation
    gcpRegions: gcpRegions
    awsEnabled: awsEnabled
    awsAccountIds: awsAccountIds
    awsRoleName: awsRoleName
    awsAzureTokenScope: awsAzureTokenScope
    awsRegions: awsRegions
    awsExportAccountId: awsExportAccountId
    awsExportBucket: awsExportBucket
    awsExportPrefix: awsExportPrefix
    awsExportName: awsExportName
    awsExportRegion: awsExportRegion
    awsCfnTemplateUrl: awsCfnTemplateUrl
    reportingCurrency: reportingCurrency
    publicWebUrl: publicWebUrl
    allowedIpRanges: allowedIpRanges
    apiMinReplicas: apiMinReplicas
    apiMaxReplicas: apiMaxReplicas
    webMinReplicas: webMinReplicas
  }
  // The apps resolve Key Vault references at revision start, so the secrets must exist.
  dependsOn: [postgres, redis]
}

output registryName string = registry.outputs.name
output registryLoginServer string = registry.outputs.loginServer
output keyVaultName string = keyVault.outputs.name
output postgresServerName string = postgres.outputs.name
output redisName string = redis.outputs.name
output apiAppName string = apps.outputs.apiName
output workerAppName string = apps.outputs.workerName
output webAppName string = apps.outputs.webName
output migrationJobName string = apps.outputs.migrationJobName
output apiUrl string = apps.outputs.apiUrl
output webUrl string = apps.outputs.webUrl
@description('Set as ACT_AZURE_PRINCIPAL_ID; this identity is granted Reader on tracked subscriptions.')
output managedIdentityPrincipalId string = identity.outputs.principalId
output managedIdentityClientId string = identity.outputs.clientId
