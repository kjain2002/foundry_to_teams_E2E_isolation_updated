# ============================================================================
# Step 1: Get the agent identity and tenant ID
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
#
# 1.1 Get a bearer token for the https://ai.azure.com audience
# 1.2 Get the agent identity client id (instance_identity.client_id)
# 1.3 Get your tenant id
#
# Writes $AgentClientId and $TenantId back into config.ps1 for the next steps.
# NOTE: 1.2 calls a project API protected by the private endpoint. Run this
#       from a client that can reach the project's private endpoint.
# ============================================================================
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/config.ps1"

Write-Host "== Step 1.1: acquire bearer token ($FoundryResource) ==" -ForegroundColor Cyan
$token = az account get-access-token --resource $FoundryResource --query accessToken -o tsv
if (-not $token) { throw "Failed to acquire token. Run 'az login' first." }

Write-Host "== Step 1.2: GET agent identity client_id ==" -ForegroundColor Cyan
$agentUrl = "$ProjectEndpoint/agents/$AgentName`?api-version=v1"
$agentJson = az rest --method get --url $agentUrl --resource $FoundryResource | ConvertFrom-Json
$clientId = $agentJson.instance_identity.client_id
if (-not $clientId) { throw "instance_identity.client_id not found. Confirm the agent has a unique identity." }
Write-Host "   agent client_id : $clientId"

Write-Host "== Step 1.3: get tenant id ==" -ForegroundColor Cyan
$tenant = az account show --query tenantId -o tsv
Write-Host "   tenant id       : $tenant"

# Persist the discovered values into config.ps1
$cfgPath = "$PSScriptRoot/config.ps1"
$cfg = Get-Content $cfgPath -Raw
$cfg = $cfg -replace '(?m)^\$AgentClientId\s*=.*$', "`$AgentClientId = `"$clientId`""
$cfg = $cfg -replace '(?m)^\$TenantId\s*=.*$',      "`$TenantId       = `"$tenant`""
Set-Content -Path $cfgPath -Value $cfg -NoNewline

Write-Host "`nSaved AgentClientId and TenantId into config.ps1." -ForegroundColor Green
Write-Host "Next: ./step2-deploy-bot.ps1"
