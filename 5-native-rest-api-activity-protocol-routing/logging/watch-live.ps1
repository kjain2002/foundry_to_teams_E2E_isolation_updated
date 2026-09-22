# ============================================================================
# Live tail of the inbound Teams channel hops for the bot.
# Run this in one window, then send a message to the published agent in Teams.
# Each poll prints the most recent ABSBotRequests rows so you can see the
# message reach the bot (ChannelToBot), the reply (BotToChannel), and any
# non-2xx result codes (where an OAuth sign-in card fails to round-trip).
#
#   ./watch-live.ps1                 # last 5 min, refreshes every 15s
#   ./watch-live.ps1 -Minutes 15     # widen the window
# ============================================================================
param(
    [int]$Minutes = 5,
    [int]$IntervalSeconds = 15
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../config.ps1"

if (-not $WorkspaceResourceId) { throw "WorkspaceResourceId empty. Run ./enable-observability.ps1 first." }

$wsGuid = az monitor log-analytics workspace show --ids $WorkspaceResourceId --query customerId -o tsv

Write-Host "Watching ABSBotRequests for bot '$BotName' (last $Minutes min). Ctrl+C to stop.`n" -ForegroundColor Cyan
while ($true) {
    $kql = @"
ABSBotRequests
| where TimeGenerated > ago(${Minutes}m)
| where Resource =~ '$BotName' or ResourceId contains '$BotName'
| project TimeGenerated, OperationName, ResultCode, ResultSignature=column_ifexists('ResultSignature',''), Error=column_ifexists('Error','')
| sort by TimeGenerated asc
"@
    Clear-Host
    Write-Host ("[{0}] last {1} min" -f (Get-Date -Format HH:mm:ss), $Minutes) -ForegroundColor DarkGray
    az monitor log-analytics query --workspace $wsGuid --analytics-query $kql -o table
    Start-Sleep -Seconds $IntervalSeconds
}
