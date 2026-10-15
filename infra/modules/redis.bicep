// Azure Cache for Redis, Basic C0: the arq job queue. Basic has no SLA or replica, which is
// fine here: a lost queue only delays work, because the scheduled jobs re-pick everything
// (ingestion is idempotent per partition). A managed service was chosen over a Redis container
// app because Container Apps TCP ingress needs a custom VNet, which this deployment avoids.
// Note: Microsoft has announced the retirement of Azure Cache for Redis in favour of Azure
// Managed Redis; see docs/deploy.md for the migration note.
//
// The access key is written into the `redis-url` Key Vault secret at deploy time; it never
// leaves Azure. Entra auth for Redis would need token refresh in the app (follow-up).

param name string
param location string
param tags object
param keyVaultName string

resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: { name: 'Basic', family: 'C', capacity: 0 }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    redisVersion: '6'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// `rediss://` makes arq (RedisSettings.from_dsn) connect with TLS on 6380.
resource redisUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'redis-url'
  properties: {
    value: 'rediss://:${redis.listKeys().primaryKey}@${redis.properties.hostName}:${redis.properties.sslPort}/0'
    contentType: 'text/plain'
  }
}

output name string = redis.name
output hostName string = redis.properties.hostName
