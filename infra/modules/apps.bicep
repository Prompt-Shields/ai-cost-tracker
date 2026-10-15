// The three container apps (api, worker, web) and the migration job.
//
// The deploy workflow rolls code in a fixed order: deploy the migration job with the new
// image, run it, and only then deploy the apps with the new image. `deployApps` and
// `deployMigrationJob` let it express those steps with this one template.

param namePrefix string
param location string
param tags object

param environmentId string
param defaultDomain string
param registryServer string
param keyVaultUri string

param identityId string
param identityClientId string
param identityPrincipalId string

@description('Deploy the api, worker and web apps (false on the bootstrap pass, before images exist).')
param deployApps bool
@description('Deploy the migration job.')
param deployMigrationJob bool

@description('Tag of the api/worker and web images the apps run.')
param appImageTag string
@description('Tag of the image the migration job runs (the commit being deployed).')
param migrationImageTag string

param entraTenantId string
param entraClientId string
param allowedGroupIds array
param roleGroups object
param connector string
param azureSubscriptionIds array

@description('Public URL of the web app if a custom domain is bound; empty uses the default FQDN.')
param publicWebUrl string

@description('CIDR ranges allowed to reach api and web ingress. Empty: open, still behind Entra sign-in.')
param allowedIpRanges array

param apiMinReplicas int
param apiMaxReplicas int
param webMinReplicas int

var apiName = '${namePrefix}-api'
var workerName = '${namePrefix}-worker'
var webName = '${namePrefix}-web'
var jobName = '${namePrefix}-migrate'

var apiUrl = 'https://${apiName}.${defaultDomain}'
var webUrl = empty(publicWebUrl) ? 'https://${webName}.${defaultDomain}' : publicWebUrl
var webOrigins = union(['https://${webName}.${defaultDomain}'], empty(publicWebUrl) ? [] : [publicWebUrl])

var backendImage = '${registryServer}/ai-cost-tracker:'
var webImage = '${registryServer}/ai-cost-tracker-web:'

var ipRestrictions = [
  for (cidr, i) in allowedIpRanges: {
    name: 'allow-${i}'
    ipAddressRange: cidr
    action: 'Allow'
  }
]

// Connection strings with credentials come from Key Vault via the managed identity.
var secrets = [
  {
    name: 'database-url'
    keyVaultUrl: '${keyVaultUri}secrets/database-url'
    identity: identityId
  }
  {
    name: 'redis-url'
    keyVaultUrl: '${keyVaultUri}secrets/redis-url'
    identity: identityId
  }
]

// Every ACT_* setting in config.py. AZURE_CLIENT_ID makes DefaultAzureCredential pick the
// user-assigned identity (with several identities it cannot guess).
var backendEnv = [
  { name: 'ACT_DATABASE_URL', secretRef: 'database-url' }
  { name: 'ACT_REDIS_URL', secretRef: 'redis-url' }
  { name: 'ACT_ENTRA_TENANT_ID', value: entraTenantId }
  { name: 'ACT_ENTRA_CLIENT_ID', value: entraClientId }
  { name: 'ACT_ALLOWED_GROUP_IDS', value: string(allowedGroupIds) }
  { name: 'ACT_ROLE_GROUPS', value: string(roleGroups) }
  { name: 'ACT_AUTH_DISABLED', value: 'false' }
  { name: 'ACT_ENVIRONMENT', value: 'production' }
  { name: 'ACT_WEB_ORIGINS', value: string(webOrigins) }
  { name: 'ACT_CONNECTOR', value: connector }
  { name: 'ACT_AZURE_SUBSCRIPTION_IDS', value: string(azureSubscriptionIds) }
  { name: 'ACT_AZURE_PRINCIPAL_ID', value: identityPrincipalId }
  { name: 'ACT_WEB_BASE_URL', value: webUrl }
  { name: 'ACT_DEFAULT_TENANT_SLUG', value: 'internal' }
  { name: 'AZURE_CLIENT_ID', value: identityClientId }
]

var identityBlock = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identityId}': {} }
}
var registries = [{ server: registryServer, identity: identityId }]

resource migrate 'Microsoft.App/jobs@2024-03-01' = if (deployMigrationJob) {
  name: jobName
  location: location
  tags: tags
  identity: identityBlock
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      replicaTimeout: 900
      replicaRetryLimit: 1
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: '${backendImage}${migrationImageTag}'
          command: ['act', 'migrate']
          env: backendEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: apiName
  location: location
  tags: tags
  identity: identityBlock
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: ipRestrictions
      }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${backendImage}${appImageTag}'
          env: backendEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '50' } } }
        ]
      }
    }
  }
}

// Exactly one worker: arq cron jobs are scheduled per process, and one is plenty for v1.
resource worker 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: workerName
  location: location
  tags: tags
  identity: identityBlock
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: '${backendImage}${appImageTag}'
          command: ['arq', 'ai_cost_tracker.worker.settings.WorkerSettings']
          env: backendEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// NEXT_PUBLIC_* values are baked into the web image at build time by the deploy workflow.
resource web 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: webName
  location: location
  tags: tags
  identity: identityBlock
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: ipRestrictions
      }
      registries: registries
    }
    template: {
      containers: [
        {
          name: 'web'
          image: '${webImage}${appImageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/', port: 3000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: webMinReplicas
        maxReplicas: 2
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '50' } } }
        ]
      }
    }
  }
}

output apiName string = apiName
output workerName string = workerName
output webName string = webName
output migrationJobName string = jobName
output apiUrl string = apiUrl
output webUrl string = webUrl
