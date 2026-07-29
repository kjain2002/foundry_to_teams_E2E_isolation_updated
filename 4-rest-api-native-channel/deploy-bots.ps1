$ErrorActionPreference = "Stop"
# Load shared config (copy config.example.ps1 -> config.ps1 and fill it in).
$configPath = Join-Path $PSScriptRoot "config.ps1"
if (-not (Test-Path $configPath)) { throw "config.ps1 not found. Run:  Copy-Item config.example.ps1 config.ps1  then edit it." }
. $configPath

$rg = $ResourceGroup
$tenant = $TenantId
$apimBase = $ApimBaseUrl
$bicep = Join-Path $PSScriptRoot "bot-service.bicep"

function Deploy-Bot($botName, $display, $msaAppId, $agentName) {
    Write-Output "=== Deploy $botName ==="
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $armId = az deployment group create `
        --resource-group $rg `
        --name "$botName-$stamp" `
        --template-file $bicep `
        --parameters botName=$botName displayName=$display msaAppId=$msaAppId tenantId=$tenant endpoint="$apimBase/$agentName/messages" `
        --query "properties.outputs.botServiceArmId.value" -o tsv 2>&1
    Write-Output "botServiceArmId = $armId"
    Write-Output ""
}

foreach ($a in $Agents) { Deploy-Bot $a.BotName $a.Display $a.BotAppId $a.RestName }
