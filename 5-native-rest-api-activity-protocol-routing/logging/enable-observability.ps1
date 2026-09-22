# ============================================================================
# Enable observability BEFORE publishing, so you can watch a Teams message
# flow end-to-end and spot where it stalls (e.g. custom MCP OAuth passthrough).
#
# Sets up two complementary layers:
#   (A) Azure Bot Service  -> Log Analytics (ABSBotRequests): inbound channel
#       hops ChannelToBot / BotToChannel, incl. the OAuth sign-in card round-trip.
#   (B) Application Insights (+ workspace): destination for Foundry server-side
#       AGENT traces (model calls, MCP tool invocations, authorization steps).
#
# (A) is fully automated here. (B) creates the App Insights resource; you then
# CONNECT it to the project once in the Foundry portal (one click) so server-side
# agent traces flow to it. See ./README.md.
#
# Run AFTER step2-deploy-bot.ps1 (needs the Bot Service to exist).
# ============================================================================
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../config.ps1"

if (-not $BotServiceArmId) { throw "BotServiceArmId is empty. Run ../step2-deploy-bot.ps1 first." }

az extension add --name application-insights --only-show-errors 2>$null | Out-Null

$location = az group show --name $ResourceGroup --query location -o tsv

Write-Host "== (1/4) Log Analytics workspace: $LogAnalyticsWorkspace ==" -ForegroundColor Cyan
try {
    $wsId = az monitor log-analytics workspace show `
        --resource-group $ResourceGroup --workspace-name $LogAnalyticsWorkspace `
        --query id -o tsv 2>$null
} catch { $wsId = $null }
if (-not $wsId) {
    $wsId = az monitor log-analytics workspace create `
        --resource-group $ResourceGroup --workspace-name $LogAnalyticsWorkspace `
        --location $location --query id -o tsv
}
Write-Host "   workspaceId : $wsId"

Write-Host "== (2/4) Application Insights: $AppInsightsName (workspace-based) ==" -ForegroundColor Cyan
try {
    $aiConn = az monitor app-insights component show `
        --app $AppInsightsName --resource-group $ResourceGroup `
        --query connectionString -o tsv 2>$null
} catch { $aiConn = $null }
if (-not $aiConn) {
    $aiConn = az monitor app-insights component create `
        --app $AppInsightsName --resource-group $ResourceGroup `
        --location $location --workspace $wsId --application-type web `
        --query connectionString -o tsv
}
Write-Host "   appInsights connectionString captured."

Write-Host "== (3/4) Bot Service diagnostic setting (BotRequest -> workspace) ==" -ForegroundColor Cyan
$logsJson = '[{"category":"BotRequest","enabled":true}]'
az monitor diagnostic-settings create `
    --name "bot-diag-to-law" `
    --resource $BotServiceArmId `
    --workspace $wsId `
    --logs $logsJson | Out-Null
Write-Host "   ABSBotRequests will now populate for the bot."

Write-Host "== (4/4) Persist ids into config.ps1 ==" -ForegroundColor Cyan
$cfgPath = "$PSScriptRoot/../config.ps1"
$cfg = Get-Content $cfgPath -Raw
$cfg = $cfg -replace '(?m)^\$WorkspaceResourceId\s*=.*$',       ('$$WorkspaceResourceId       = "' + $wsId + '"')
$cfg = $cfg -replace '(?m)^\$AppInsightsConnectionString\s*=.*$', ('$$AppInsightsConnectionString = "' + $aiConn + '"')
Set-Content -Path $cfgPath -Value $cfg -NoNewline

Write-Host "`nDone." -ForegroundColor Green
Write-Host "NEXT - connect App Insights to the project so server-side AGENT traces flow:" -ForegroundColor Yellow
Write-Host "  Foundry portal -> project '$FoundryProject' -> Observability / Tracing"
Write-Host "  -> Connect Application Insights -> pick '$AppInsightsName'."
Write-Host "Then watch live with: ./watch-live.ps1   (and use trace-queries.kql)"
