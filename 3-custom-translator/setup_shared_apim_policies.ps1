# Set routing policies on the shared APIM operations.
# ── SET THESE for your environment ────────────────────────────
$rg   = "<your-resource-group>"
$apim = "<your-apim-name>"
$api  = "foundry-bot"
$sub  = "<your-subscription-id>"
$backend = "https://<your-shared-container>.<cae-default-domain>"
# ──────────────────────────────────────────────
$root = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim/apis/$api"

function Set-OpPolicy($opId, $xml) {
    $body = @{ properties = @{ format = "rawxml"; value = $xml } } | ConvertTo-Json -Depth 8
    $f = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($f, $body, (New-Object System.Text.UTF8Encoding($false)))
    $uri = "$root/operations/$opId/policies/policy?api-version=2024-05-01"
    az rest --method put --uri $uri --headers "Content-Type=application/json" --body "@$f" -o none
    Write-Host ("policy {0}: exit {1}" -f $opId, $LASTEXITCODE)
    Remove-Item $f -Force
}

$msgXml   = '<policies><inbound><base /><set-backend-service base-url="__U__" /><rewrite-uri template="/api/messages" copy-unmatched-params="false" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)
$startXml = '<policies><inbound><set-backend-service base-url="__U__" /><rewrite-uri template="/api/oauth/start" copy-unmatched-params="true" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)
$cbXml    = '<policies><inbound><set-backend-service base-url="__U__" /><rewrite-uri template="/api/oauth/callback" copy-unmatched-params="true" /></inbound><backend><base /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'.Replace('__U__', $backend)

Set-OpPolicy "post-agent-templated" $msgXml
Set-OpPolicy "oauth-start" $startXml
Set-OpPolicy "oauth-callback" $cbXml
