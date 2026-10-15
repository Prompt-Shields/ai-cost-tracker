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
