# ============================================================================
# Step 2: Create the Azure Bot Service resource
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
#
# Deploys bot-service.bicep to the resource group that contains the Foundry
# resource, then captures the Bot Service ARM id used in Step 4.
# ============================================================================
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/config.ps1"

if (-not $AgentClientId) { throw "AgentClientId is empty. Run ./step1-get-identity.ps1 first." }
if (-not $TenantId)      { throw "TenantId is empty. Run ./step1-get-identity.ps1 first." }

Write-Host "== Set subscription context ==" -ForegroundColor Cyan
az account set --subscription $SubscriptionId

Write-Host "== Register Microsoft.BotService provider ==" -ForegroundColor Cyan
az provider register --namespace Microsoft.BotService | Out-Null

Write-Host "== Step 2: deploy bot-service.bicep ==" -ForegroundColor Cyan
Write-Host "   botName  : $BotName"
Write-Host "   endpoint : $ActivityEndpoint"
az deployment group create `
  --resource-group $ResourceGroup `
  --template-file "$PSScriptRoot/bot-service.bicep" `
  --parameters `
      botName=$BotName `
      displayName="$AgentDisplayName" `
      msaAppId=$AgentClientId `
      tenantId=$TenantId `
      endpoint=$ActivityEndpoint | Out-Null

Write-Host "== Capture Bot Service ARM id ==" -ForegroundColor Cyan
$botArmId = az bot show --name $BotName --resource-group $ResourceGroup --query id -o tsv
if (-not $botArmId) { throw "Failed to read Bot Service ARM id." }
Write-Host "   botServiceArmId : $botArmId"

# Persist for Step 4
$cfgPath = "$PSScriptRoot/config.ps1"
$cfg = Get-Content $cfgPath -Raw
if ($cfg -match '(?m)^\$BotServiceArmId\s*=') {
    $cfg = $cfg -replace '(?m)^\$BotServiceArmId\s*=.*$', "`$BotServiceArmId = `"$botArmId`""
} else {
    $cfg = $cfg.TrimEnd() + "`r`n`$BotServiceArmId = `"$botArmId`"`r`n"
}
Set-Content -Path $cfgPath -Value $cfg -NoNewline

Write-Host "`nSaved BotServiceArmId into config.ps1." -ForegroundColor Green
Write-Host "Next: ./step3-enable-activity.ps1"
