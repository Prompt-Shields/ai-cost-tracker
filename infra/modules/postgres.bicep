// Azure Database for PostgreSQL Flexible Server (PG16) with TimescaleDB, plus the
// `database-url` Key Vault secret the apps read.
//
// Password handling: the deploy workflow generates a random password only when the server
// does not exist yet (or when asked to rotate) and passes it as a secure parameter. On every
// other deploy the parameter is empty, so the password and the Key Vault secrets are left
// untouched. Unverified assumption (issue #2): the Flexible Server RP keeps the existing
// admin password when `administratorLoginPassword` is omitted on update.

param name string
param location string
param tags object

@description('Key Vault that receives the admin password and the app connection string.')
param keyVaultName string

@description('Admin password. Empty on routine deploys: leaves the password and secrets as they are.')
@secure()
param administratorPassword string = ''

param administratorLogin string = 'act'
param databaseName string = 'ai_cost_tracker'
param skuName string = 'Standard_B1ms'
param skuTier string = 'Burstable'
param storageSizeGB int = 32
param backupRetentionDays int = 7

var setPassword = !empty(administratorPassword)

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: skuName, tier: skuTier }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: setPassword ? administratorPassword : null
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Enabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

// Allow-list the extension so `CREATE EXTENSION timescaledb` (migration 0001) is permitted.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: { value: 'TIMESCALEDB', source: 'user-override' }
}

// TimescaleDB must be preloaded. Changing this needs a server restart, which the deploy
// workflow performs when the server reports a pending restart. Configuration writes on one
// server must be serial, hence the explicit dependency.
resource preload 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'shared_preload_libraries'
  properties: { value: 'pg_stat_statements,timescaledb', source: 'user-override' }
  dependsOn: [extensions]
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
  dependsOn: [preload]
}

// Container Apps egress has no fixed IP without a custom VNet, so allow Azure-internal
// traffic (the special 0.0.0.0 rule). TLS is enforced by the server. Private networking is a
// follow-up (docs/deploy.md).
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: server
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
  dependsOn: [database]
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource passwordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (setPassword) {
  parent: vault
  name: 'postgres-admin-password'
  properties: { value: administratorPassword, contentType: 'text/plain' }
}

// asyncpg needs `ssl=require` in the URL; the password is hex, so it needs no URL escaping.
resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (setPassword) {
  parent: vault
  name: 'database-url'
  properties: {
    value: 'postgresql+asyncpg://${administratorLogin}:${administratorPassword}@${server.properties.fullyQualifiedDomainName}:5432/${databaseName}?ssl=require'
    contentType: 'text/plain'
  }
}

output name string = server.name
output fqdn string = server.properties.fullyQualifiedDomainName
