$ErrorActionPreference = "Stop"
# --- Fill in your Foundry account + project and target subscription/resource group ---
$acct = "<your-foundry-account>"; $proj = "<your-project>"
$ep = "https://$acct.services.ai.azure.com/api/projects/$proj"
$res = "https://ai.azure.com"
$rgId = "/subscriptions/<your-subscription-id>/resourceGroups/<your-resource-group>/providers/Microsoft.BotService/botServices"
$tmp = Join-Path $PSScriptRoot "_tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Publish-Agent($agentName, $display, $botName, $short, $full) {
    Write-Output "=== Publishing $agentName (scope=Shared) ==="
    $body = [ordered]@{
        agentDisplayName    = $display
        botServiceArmId     = "$rgId/$botName"
        publishScope        = "Shared"
        publishAsAutopilot  = $false
        appVersion          = "1.0.0"
        shortDescription    = $short
        fullDescription     = $full
        developerName       = "<your-org>"
        developerWebsiteUrl = "https://www.example.com"
        privacyUrl          = "https://www.example.com/privacy"
        termsOfUseUrl       = "https://www.example.com/terms"
    }
    $bp = Join-Path $tmp "publish-$agentName.json"
    ($body | ConvertTo-Json -Depth 10) | Set-Content -Path $bp -Encoding utf8
    $out = az rest --method post --url "$ep/agents/$agentName/microsoft365/publish`?api-version=v1" --resource $res --headers "Content-Type=application/json" --body "@$bp" 2>&1
    Write-Output $out
    Write-Output ""
}

Publish-Agent "search-agent-restapi" "Search Agent (REST API)" "bot-search-agent-restapi" `
    "Grounded Q&A over indexed docs" "Azure AI Search grounded agent published to Teams via the native Foundry REST API."

Publish-Agent "data-agent-restapi" "Data Agent (REST API)" "bot-data-agent-restapi" `
    "Live data via an MCP tool" "MCP-tool agent published to Teams via the native Foundry REST API (MCP tool demo)."
