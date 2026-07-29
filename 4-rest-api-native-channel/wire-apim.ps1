$ErrorActionPreference = "Stop"
# Load shared config (copy config.example.ps1 -> config.ps1 and fill it in).
$configPath = Join-Path $PSScriptRoot "config.ps1"
if (-not (Test-Path $configPath)) { throw "config.ps1 not found. Run:  Copy-Item config.example.ps1 config.ps1  then edit it." }
. $configPath

$sub = $SubscriptionId
$rg = $ResourceGroup
$apim = $ApimName
$api = $ApimApiName
$apiVer = "2023-05-01-preview"
$proj = $FoundryProject
$armTok = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$H = @{ Authorization = "Bearer $armTok"; "Content-Type" = "application/json" }
$mgmt = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.ApiManagement/service/$apim"
$snapshotDir = $PSScriptRoot

# ---- 1. Snapshot + update API-level validate-jwt audience list ----
$polUri = "$mgmt/apis/$api/policies/policy?format=rawxml&api-version=$apiVer"
$cur = (Invoke-RestMethod -Uri $polUri -Headers $H).properties.value
Set-Content -Path (Join-Path $snapshotDir "apim-api-policy.original.xml") -Value $cur -Encoding utf8
Write-Output "Snapshot saved: apim-api-policy.original.xml"

$newAuds = @($Agents | ForEach-Object { $_.BotAppId })
$insert = ($newAuds | ForEach-Object { "                                        <value>$_</value>" }) -join "`n"
if ($cur -match [regex]::Escape($newAuds[0])) {
    Write-Output "Audiences already present; skipping policy edit."
}
else {
    $updated = $cur -replace "(?s)(\s*)</claim>", "`n$insert`$1</claim>"
    $body = @{ properties = @{ value = $updated; format = "rawxml" } } | ConvertTo-Json -Depth 10
    Invoke-RestMethod -Uri $polUri -Headers $H -Method Put -Body $body | Out-Null
    Write-Output "API-level policy updated with $($newAuds.Count) new audiences."
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

foreach ($a in $Agents) { Add-NativeOp $a.RestName }
Write-Output "Done."
