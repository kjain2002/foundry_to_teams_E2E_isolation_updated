// =========================================================================
// publish-agent.bicep — wires one Foundry agent to one Teams bot via APIM
// =========================================================================
// Inputs: existing APIM, existing Foundry agent's agentId, a fresh AAD AppId.
// Outputs: Bot Service + Teams channel + APIM operation routing to that agent.
//
// AAD app creation is NOT done in Bicep (Microsoft.Graph provider is still
// preview-only and unreliable for clients) — the PowerShell driver creates
// the app first via `az ad app create`, then passes the AppId in here.
// =========================================================================

@description('Bot Service resource name (lowercase, hyphens). e.g. bot-sales-copilot.')
param botName string

@description('Teams display name shown to users. e.g. "Sales Copilot".')
param botDisplayName string

@description('Entra (AAD) AppId of the SingleTenant app created by the driver script.')
param botAppId string

@description('Entra tenant id the app belongs to.')
param botTenantId string

@description('Existing APIM resource name (from bootstrap phase).')
param apimName string

@description('APIM API name created by bootstrap.bicep.')
param foundryBotApiName string = 'foundry-bot'

@description('Foundry project name. Retained for naming/tagging; routing no longer uses it (translator container holds the Foundry endpoint).')
#disable-next-line no-unused-params
param foundryProjectName string

@description('Foundry agent id to publish. For Prompt Agents this is the display name (e.g. "<your-agent>"). For legacy Assistants it is asst_xxx.')
param agentId string

@description('Tags applied to the Bot Service.')
param tags object = {}

// -------------------------------------------------------------------------
// Translator Container App inputs (outputs of bootstrap-translator.bicep)
// -------------------------------------------------------------------------
@description('Container Apps Environment id (internal, VNet-injected).')
param containerAppsEnvironmentId string

@description('Default domain of the Container Apps Environment (used to build the internal FQDN).')
param containerAppsDefaultDomain string

@description('ACR login server (e.g. acrtranslatorabc.azurecr.io).')
param acrLoginServer string

@description('User-assigned MI resource id used by the Container App for ACR pull, Foundry, Table, and KV.')
param translatorMiResourceId string

@description('Client id of the translator MI (used to pin DefaultAzureCredential inside the container).')
param translatorMiClientId string

@description('Translator container image, including tag (e.g. acrtranslatorabc.azurecr.io/translator:abc1234).')
param translatorImage string

@description('Foundry project endpoint, e.g. https://<your-foundry-account>.services.ai.azure.com/api/projects/<your-project>')
param foundryProjectEndpoint string

@description('Table Storage endpoint (e.g. https://sttranslatorabc.table.core.windows.net/) used for thread state.')
param threadTableUrl string

@description('Key Vault secret URI (unversioned) for this bot\'s client secret (e.g. https://kv-abc.vault.azure.net/secrets/bot-myagent). Not a secret value itself.')
#disable-next-line secure-secrets-in-params
param botSecretKvUri string

@description('Container App name. Defaults to ca-<botName>.')
param containerAppName string = 'ca-${botName}'

// -------------------------------------------------------------------------
// Existing APIM
// -------------------------------------------------------------------------
resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource foundryBotApi 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  name: foundryBotApiName
  parent: apim
}

// -------------------------------------------------------------------------
// APIM operation for this specific agent.
// Endpoint exposed to Bot Channel Service:
//     POST https://<apim>.azure-api.net/bot/agents/<agentId>/messages
// -------------------------------------------------------------------------
resource agentOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  name: 'post-agent-${take(replace(agentId, '_', '-'), 60)}'
  parent: foundryBotApi
  properties: {
    displayName: 'POST messages → ${agentId}'
    method: 'POST'
    urlTemplate: '/agents/${agentId}/messages'
    request: {
      description: 'Activity Protocol message from Bot Channel Service'
    }
    responses: [
      {
        statusCode: 200
        description: 'OK'
      }
      {
        statusCode: 401
        description: 'Unauthorized (JWT validation failed)'
      }
    ]
  }
}

// -------------------------------------------------------------------------
// Per-bot translator Container App.
// Ingress is set to `external: true` so envoy will accept requests from
// non-CAE callers inside the VNet (APIM). The CAE itself is still internal
// (no public IP — staticIp is the VNet ILB), so traffic never leaves the VNet.
// If `external` were `false`, envoy 404s any caller that isn't another
// container app in the same CAE, even when the .internal. FQDN resolves.
// See: https://learn.microsoft.com/azure/container-apps/connect-apps#external-and-internal-fqdns
// -------------------------------------------------------------------------
var translatorInternalFqdn = '${containerAppName}.${containerAppsDefaultDomain}'
var translatorInternalUrl = 'https://${translatorInternalFqdn}'

resource translatorApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: containerAppName
  location: resourceGroup().location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${translatorMiResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true                     // VNet-only CAE; needed so envoy accepts APIM (non-CAE) callers
        targetPort: 3978
        transport: 'auto'
        allowInsecure: false
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
      registries: [
        {
          server: acrLoginServer
          identity: translatorMiResourceId
        }
      ]
      secrets: [
        {
          name: 'bot-app-password'
          keyVaultUrl: botSecretKvUri
          identity: translatorMiResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'translator'
          image: translatorImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'BOT_APP_ID', value: botAppId }
            { name: 'BOT_APP_PASSWORD', secretRef: 'bot-app-password' }
            { name: 'BOT_APP_TENANT_ID', value: botTenantId }
            { name: 'BOT_APP_TYPE', value: 'SingleTenant' }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'FOUNDRY_AGENT_NAME', value: agentId }
            { name: 'THREAD_TABLE_URL', value: threadTableUrl }
            { name: 'THREAD_TABLE_NAME', value: 'threads' }
            // Pin DefaultAzureCredential to the user-assigned MI.
            { name: 'AZURE_CLIENT_ID', value: translatorMiClientId }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 3978 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

// Substitute translator URL into the policy template at deploy time.
var operationPolicyXml = replace(
  loadTextContent('./policies/operation-per-agent-translator.xml'),
  '__TRANSLATOR_URL__',
  translatorInternalUrl
)

resource agentOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  name: 'policy'
  parent: agentOperation
  properties: {
    format: 'rawxml'
    value: operationPolicyXml
  }
}

// Append this bot's AppId to the allowed-audiences named value so the API
// base policy will accept its JWT. The driver script reads the current value
// and passes the merged list in via `existingAllowedAudiences`.
@description('Comma-separated list of all bot AppIds that should be accepted by the API. The driver merges existing + new before passing in.')
param mergedAllowedAudiences string

resource allowedAudiences 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  name: 'allowed-bot-audiences'
  parent: apim
  properties: {
    displayName: 'allowed-bot-audiences'
    value: mergedAllowedAudiences
    secret: false
  }
}

// -------------------------------------------------------------------------
// Azure Bot Service (F0 — free) pointing at the APIM operation above.
// -------------------------------------------------------------------------
resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  tags: tags
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: botDisplayName
    endpoint: 'https://${apim.name}.azure-api.net/bot/agents/${agentId}/messages'
    msaAppId: botAppId
    msaAppTenantId: botTenantId
    msaAppType: 'SingleTenant'
    publicNetworkAccess: 'Enabled'  // ingress from Bot Channel Service is public
    isStreamingSupported: false
  }
  dependsOn: [
    agentOperationPolicy
    allowedAudiences
  ]
}

// Teams channel on the bot.
resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  name: 'MsTeamsChannel'
  parent: bot
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
      enableCalling: false
      acceptedTerms: true
    }
  }
}

output botResourceId string = bot.id
output botEndpoint string = bot.properties.endpoint
output botName string = bot.name
output translatorAppName string = translatorApp.name
output translatorInternalUrl string = translatorInternalUrl
