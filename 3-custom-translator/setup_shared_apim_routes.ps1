# One-time: create shared APIM routes for the shared-router container.
# - templated messaging op /agents/{agentId}/messages -> container /api/messages (keeps bot-JWT validation)
# - /api/oauth/start + /api/oauth/callback -> container (skip bot-JWT; browser has no bot token)
$ErrorActionPreference = "Stop"
# ── SET THESE for your environment ────────────────────────────
$rg   = "<your-resource-group>"
$apim = "<your-apim-name>"
$api  = "foundry-bot"
$sub  = "<your-subscription-id>"
$backend = "https://<your-shared-container>.<cae-default-domain>"
# ──────────────────────────────────────────────
$root = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim/apis/$api"

function Put-Json($uri, $obj) {
    $body = $obj | ConvertTo-Json -Depth 8
    $f = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
    az rest --method put --uri $uri --headers "Content-Type=application/json" --body "@$f" -o none
    $code = $LASTEXITCODE
    Remove-Item $f -Force
    return $code
}

function New-Op($opId, $display, $method, $url, $params) {
    $r = Put-Json "$root/operations/$opId`?api-version=2024-05-01" @{ properties = @{ displayName = $display; method = $method; urlTemplate = $url; templateParameters = $params; responses = @() } }
    Write-Host ("op {0}: exit {1}" -f $opId, $r)
}
function Set-OpPolicy($opId, $xml) {
    $r = Put-Json "$root/operations/$opId/policies/policy`?api-version=2024-05-01" @{ properties = @{ format = "rawxml"; value = $xml } }
    Write-Host ("policy {0}: exit {1}" -f $opId, $r)
}

$msgXml   = '<policies><inbound><base /><set-backend-service base-url="__U__" /><rewrite-uri template="/api/messages" copy-unmatched-params="false" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)
$startXml = '<policies><inbound><set-backend-service base-url="__U__" /><rewrite-uri template="/api/oauth/start" copy-unmatched-params="true" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)
$cbXml    = '<policies><inbound><set-backend-service base-url="__U__" /><rewrite-uri template="/api/oauth/callback" copy-unmatched-params="true" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)

New-Op "post-agent-templated" "POST messages -> any agent" "POST" "/agents/{agentId}/messages" @(@{ name = "agentId"; description = "Foundry agent id"; type = "string"; required = $true })
Set-OpPolicy "post-agent-templated" $msgXml

New-Op "oauth-start" "OAuth start (sign-in redirect)" "GET" "/api/oauth/start" @()
Set-OpPolicy "oauth-start" $startXml

New-Op "oauth-callback" "OAuth callback (code exchange)" "GET" "/api/oauth/callback" @()
Set-OpPolicy "oauth-callback" $cbXml

Write-Host "----- operations now -----"
az apim api operation list -g $rg --service-name $apim --api-id $api --query "[].{name:name,method:method,url:urlTemplate}" -o table
