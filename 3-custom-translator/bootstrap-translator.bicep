// =========================================================================
// bootstrap-translator.bicep — one-time shared infra for translator containers
// =========================================================================
// Provisions the things every per-bot Container App depends on:
//   - Azure Container Registry (private, MI-pulled)
//   - Log Analytics workspace
//   - Container Apps Environment (VNet-injected, internal-only ingress)
//   - Storage account + private endpoint (for the threadId Table)
//   - User-assigned managed identity ("translator-mi") with RBAC on:
//       * Foundry account (Cognitive Services User)
//       * ACR          (AcrPull)
//       * Storage      (Storage Table Data Contributor)
//       * Key Vault    (Key Vault Secrets User) — to read bot client secrets
//
// Re-run is safe: idempotent. Skip if these already exist in the RG.
// =========================================================================

@description('Region for all resources. Should match the Foundry/APIM region.')
param location string = resourceGroup().location

@description('Existing VNet name (the one that hosts the Foundry PE + APIM).')
param vnetName string

@description('Name of an EXISTING /23 (or larger) subnet to inject the Container Apps Environment into. Must have no NSG rules blocking ACA-required ports.')
param acaSubnetName string

@description('Existing subnet name where private endpoints (storage, ACR) will be placed.')
param peSubnetName string

@description('Foundry account name. Used to grant the translator MI RBAC on it.')
param foundryAccountName string

@description('Existing Key Vault name where bot client secrets are stored.')
param keyVaultName string

@description('Suffix for globally-unique names (ACR, storage).')
param uniqueSuffix string

@description('Name of the Container Apps Environment.')
param caeName string = 'cae-translator-${uniqueSuffix}'

@description('Name of the ACR (5-50 alphanumerics, globally unique).')
param acrName string = 'acrtranslator${uniqueSuffix}'

@description('Storage account name (3-24 lowercase alphanumerics, globally unique).')
param storageName string = 'sttranslator${uniqueSuffix}'

@description('Log Analytics workspace name.')
param logWorkspaceName string = 'log-translator-${uniqueSuffix}'

@description('Translator user-assigned managed identity name.')
param translatorMiName string = 'mi-translator-${uniqueSuffix}'

@description('Tags applied to all resources.')
param tags object = {}

// -------------------------------------------------------------------------
// Existing references
// -------------------------------------------------------------------------
resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' existing = {
  name: vnetName
}

resource acaSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' existing = {
  name: acaSubnetName
  parent: vnet
}

resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-01-01' existing = {
  name: peSubnetName
  parent: vnet
}

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryAccountName
}

resource kv 'Microsoft.KeyVault/vaults@2024-04-01-preview' existing = {
  name: keyVaultName
}

// -------------------------------------------------------------------------
// User-assigned MI used by every translator Container App
// -------------------------------------------------------------------------
resource translatorMi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: translatorMiName
  location: location
  tags: tags
}

// -------------------------------------------------------------------------
// Log Analytics (CAE requirement)
// -------------------------------------------------------------------------
resource logWs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// -------------------------------------------------------------------------
// Azure Container Registry — private endpoint only, MI auth
// -------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: { name: 'Premium' }   // Premium required for private endpoints
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Disabled'
    networkRuleBypassOptions: 'AzureServices'
  }
}

// -------------------------------------------------------------------------
// Storage account — hosts the threadId Table; private endpoint only
// -------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false   // MI-only
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  name: 'default'
  parent: storage
}

resource threadsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  name: 'threads'
  parent: tableService
}

// -------------------------------------------------------------------------
// Container Apps Environment — VNet-injected, internal ingress
// -------------------------------------------------------------------------
resource cae 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: caeName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logWs.properties.customerId
        sharedKey: logWs.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: acaSubnet.id
      internal: true            // only reachable from inside the VNet
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

// -------------------------------------------------------------------------
// Private endpoints (ACR + Storage Table)
// -------------------------------------------------------------------------
resource acrPe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${acr.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: 'acr'
        properties: {
          privateLinkServiceId: acr.id
          groupIds: [ 'registry' ]
        }
      }
    ]
  }
}

resource storagePe 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${storage.name}-table'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnet.id }
    privateLinkServiceConnections: [
      {
        name: 'table'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: [ 'table' ]
        }
      }
    ]
  }
}

// NOTE: DNS zone groups for the private endpoints above are intentionally
// NOT created here — your existing private DNS zones (privatelink.azurecr.io,
// privatelink.table.core.windows.net) should be linked to the VNet already.
// If they aren't, add them and create privateDnsZoneGroups on each PE.

// -------------------------------------------------------------------------
// RBAC for the translator MI
// -------------------------------------------------------------------------
// Cognitive Services User on Foundry
resource roleCogSvcUser 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: 'a97b65f3-24c7-4388-baec-2e87135dc908'
}

resource raFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, translatorMi.id, roleCogSvcUser.id)
  scope: foundry
  properties: {
    roleDefinitionId: roleCogSvcUser.id
    principalId: translatorMi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// AcrPull on ACR
resource roleAcrPull 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

resource raAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, translatorMi.id, roleAcrPull.id)
  scope: acr
  properties: {
    roleDefinitionId: roleAcrPull.id
    principalId: translatorMi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Table Data Contributor on storage
resource roleTableContrib 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

resource raTable 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, translatorMi.id, roleTableContrib.id)
  scope: storage
  properties: {
    roleDefinitionId: roleTableContrib.id
    principalId: translatorMi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Key Vault Secrets User on KV (to read bot client secrets)
resource roleKvSecretsUser 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource raKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, translatorMi.id, roleKvSecretsUser.id)
  scope: kv
  properties: {
    roleDefinitionId: roleKvSecretsUser.id
    principalId: translatorMi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// -------------------------------------------------------------------------
// Outputs consumed by publish-agent.bicep
// -------------------------------------------------------------------------
output caeId string = cae.id
output caeName string = cae.name
output caeDefaultDomain string = cae.properties.defaultDomain
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output storageAccountName string = storage.name
output tableEndpoint string = storage.properties.primaryEndpoints.table
output translatorMiId string = translatorMi.id
output translatorMiClientId string = translatorMi.properties.clientId
output translatorMiPrincipalId string = translatorMi.properties.principalId
