$ErrorActionPreference = "Stop"
# --- Fill in your resource group / tenant / APIM base URL ---
$rg = "<your-resource-group>"
$tenant = "<your-tenant-id>"
$apimBase = "https://<your-apim-name>.azure-api.net/bot/agents"
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

Deploy-Bot "bot-search-agent-restapi" "Search Agent (REST API)" "<bot-app-id-1>" "search-agent-restapi"
Deploy-Bot "bot-data-agent-restapi" "Data Agent (REST API)" "<bot-app-id-2>" "data-agent-restapi"
