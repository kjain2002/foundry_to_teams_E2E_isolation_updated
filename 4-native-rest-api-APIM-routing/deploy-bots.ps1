$ErrorActionPreference = "Stop"
# Load shared config (copy config.example.ps1 -> config.ps1 and fill it in).
$configPath = Join-Path $PSScriptRoot "config.ps1"
if (-not (Test-Path $configPath)) { throw "config.ps1 not found. Run:  Copy-Item config.example.ps1 config.ps1  then edit it." }
. $configPath

$rg = $ResourceGroup
$tenant = $TenantId
$ep = $ProjectEndpoint
$bicep = Join-Path $PSScriptRoot "bot-service.bicep"

function Deploy-Bot($botName, $display, $msaAppId, $agentName) {
    Write-Output "=== Deploy $botName ==="
    # Doc-exact: the bot messaging endpoint is Foundry's activity-protocol route directly (no APIM).
    $endpoint = "$ep/agents/$agentName/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $armId = az deployment group create `
        --resource-group $rg `
        --name "$botName-$stamp" `
        --template-file $bicep `
        --parameters botName=$botName displayName=$display msaAppId=$msaAppId tenantId=$tenant endpoint="$endpoint" `
        --query "properties.outputs.botServiceArmId.value" -o tsv 2>&1
    Write-Output "botServiceArmId = $armId"
    Write-Output ""
}

foreach ($a in $Agents) { Deploy-Bot $a.BotName $a.Display $a.BotAppId $a.RestName }
