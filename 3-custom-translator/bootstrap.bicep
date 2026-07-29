// =========================================================================
// bootstrap.bicep — one-time APIM bridge for a privately-networked Foundry
// =========================================================================
// Provisions Azure API Management in External VNet mode so that:
//   - Microsoft Bot Channel Service can reach it from the public internet
//   - APIM can reach the Foundry private endpoint via the linked private DNS
//
// Idempotent: re-running updates policies and tags without rebuilding APIM.
// Skip this file entirely if your tenant already has an APIM fronting Foundry.
// =========================================================================

@description('Region for APIM. Must match the Foundry VNet region.')
param location string = resourceGroup().location

@description('APIM resource name. Globally unique.')
param apimName string

@description('Publisher email shown in the APIM developer portal.')
param publisherEmail string

@description('Publisher org name shown in the APIM developer portal.')
param publisherName string

@description('APIM SKU. Developer = $50/mo for POC. StandardV2 or Premium for prod.')
@allowed([
  'Developer'
  'StandardV2'
  'Premium'
])
param apimSku string = 'Developer'

@description('Capacity units. 1 is fine for Developer/StandardV2; scale for Premium.')
param apimCapacity int = 1

@description('Name of the existing VNet that hosts the Foundry private endpoint.')
param vnetName string

@description('Name of the subnet APIM will inject its NIC into. Must be a /28 or larger, with NSG allowing APIM-required ports.')
param apimSubnetName string

@description('Foundry account name. APIM backend resolves to <name>.services.ai.azure.com.')
param foundryAccountName string

@description('Tags applied to APIM.')
param tags object = {}

// -------------------------------------------------------------------------
// Existing references
// -------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' existing = {
  name: vnetName
}

resource apimSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' existing = {
  name: apimSubnetName
  parent: vnet
}

// -------------------------------------------------------------------------
// API Management — External VNet mode
// -------------------------------------------------------------------------
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: apimSku
    capacity: apimCapacity
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: {
      subnetResourceId: apimSubnet.id
    }
  }
}

// -------------------------------------------------------------------------
// Named values — bot AppIds are appended per-agent by publish-agent.bicep.
// This base policy fragment trusts any AppId listed in the namedValue below.
// -------------------------------------------------------------------------
resource allowedAudiences 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  name: 'allowed-bot-audiences'
  parent: apim
  properties: {
    displayName: 'allowed-bot-audiences'
    value: '00000000-0000-0000-0000-000000000000' // placeholder; publish-agent.bicep replaces
    secret: false
  }
}

resource foundryBackendUrl 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  name: 'foundry-backend-url'
  parent: apim
  properties: {
    displayName: 'foundry-backend-url'
    value: 'https://${foundryAccountName}.services.ai.azure.com'
    secret: false
  }
}

// -------------------------------------------------------------------------
// Base API — single "foundry-bot" API; per-agent operations are added later.
// Backend = Foundry; APIM resolves it via the linked private DNS zone.
// -------------------------------------------------------------------------
resource foundryBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  name: 'foundry-private'
  parent: apim
  properties: {
    protocol: 'http'
    url: 'https://${foundryAccountName}.services.ai.azure.com'
    tls: {
      validateCertificateChain: true
      validateCertificateName: true
    }
  }
}

resource foundryBotApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  name: 'foundry-bot'
  parent: apim
  properties: {
    displayName: 'Foundry Bot Bridge'
    path: 'bot'
    protocols: [ 'https' ]
    subscriptionRequired: false  // Bot Channel Service auth is via AAD JWT, not APIM key
    serviceUrl: 'https://${foundryAccountName}.services.ai.azure.com'
  }
}

// API-level inbound policy: validate Bot AAD JWT, set backend.
// Per-agent operations only need to rewrite the URI to the right agent path.
resource baseApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  name: 'policy'
  parent: foundryBotApi
  properties: {
    format: 'rawxml'
    value: loadTextContent('./policies/api-base.xml')
  }
  dependsOn: [
    allowedAudiences
    foundryBackendUrl
    foundryBackend
  ]
}

output apimResourceId string = apim.id
output apimGatewayUrl string = 'https://${apim.name}.azure-api.net'
output foundryBotApiName string = foundryBotApi.name
output apimPrincipalId string = apim.identity.principalId
