// User-assigned managed identity shared by the api, worker, web and migration job.
// It pulls images from ACR, reads Key Vault secrets, and is the principal that gets `Reader`
// on each tracked subscription (ARCHITECTURE.md §4.3), so cost/metrics/inventory reads need
// no secret. Its principalId becomes ACT_AZURE_PRINCIPAL_ID.

@description('Resource name of the identity.')
param name string

param location string
param tags object

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output name string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
