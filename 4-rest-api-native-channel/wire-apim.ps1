$ErrorActionPreference = "Stop"
# --- Fill in your subscription / resource group / APIM / project ---
$sub = "<your-subscription-id>"
$rg = "<your-resource-group>"
$apim = "<your-apim-name>"
$api = "foundry-bot"
$apiVer = "2023-05-01-preview"
$proj = "<your-project>"
$armTok = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$H = @{ Authorization = "Bearer $armTok"; "Content-Type" = "application/json" }
$mgmt = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim"
$snapshotDir = $PSScriptRoot

# ---- 1. Snapshot + update API-level validate-jwt audience list ----
$polUri = "$mgmt/apis/$api/policies/policy?format=rawxml&api-version=$apiVer"
$cur = (Invoke-RestMethod -Uri $polUri -Headers $H).properties.value
Set-Content -Path (Join-Path $snapshotDir "apim-api-policy.original.xml") -Value $cur -Encoding utf8
Write-Output "Snapshot saved: apim-api-policy.original.xml"

$newAuds = @("<bot-app-id-1>", "<bot-app-id-2>")
$insert = ($newAuds | ForEach-Object { "                                        <value>$_</value>" }) -join "`n"
if ($cur -match [regex]::Escape($newAuds[0])) {
    Write-Output "Audiences already present; skipping policy edit."
}
else {
    $updated = $cur -replace "(?s)(\s*)</claim>", "`n$insert`$1</claim>"
    $body = @{ properties = @{ value = $updated; format = "rawxml" } } | ConvertTo-Json -Depth 10
    Invoke-RestMethod -Uri $polUri -Headers $H -Method Put -Body $body | Out-Null
    Write-Output "API-level policy updated with 2 new audiences."
}

# ---- 2. Add native pass-through operation + policy per copy ----
function Add-NativeOp($agentName) {
    $opId = "post-agent-$agentName"
    $opUri = "$mgmt/apis/$api/operations/$opId`?api-version=$apiVer"
    $opBody = @{
        properties = @{
            displayName = "POST messages ($agentName) [native]"
            method      = "POST"
            urlTemplate = "/agents/$agentName/messages"
            templateParameters = @()
            responses   = @()
        }
    } | ConvertTo-Json -Depth 10
    Invoke-RestMethod -Uri $opUri -Headers $H -Method Put -Body $opBody | Out-Null

    $rewrite = "/api/projects/$proj/agents/$agentName/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"
    $polXml = @"
<policies>
    <inbound>
        <base />
        <rewrite-uri template="$rewrite" copy-unmatched-params="false" />
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>
"@
    $opPolUri = "$mgmt/apis/$api/operations/$opId/policies/policy?api-version=$apiVer"
    $opPolBody = @{ properties = @{ value = $polXml; format = "rawxml" } } | ConvertTo-Json -Depth 10
    Invoke-RestMethod -Uri $opPolUri -Headers $H -Method Put -Body $opPolBody | Out-Null
    Write-Output "Operation + policy set for $agentName (native activity route)."
}

Add-NativeOp "search-agent-restapi"
Add-NativeOp "data-agent-restapi"
Write-Output "Done."
