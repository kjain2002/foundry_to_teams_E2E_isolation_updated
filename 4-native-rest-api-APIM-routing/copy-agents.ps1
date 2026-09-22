$ErrorActionPreference = "Stop"
# Load shared config (copy config.example.ps1 -> config.ps1 and fill it in).
$configPath = Join-Path $PSScriptRoot "config.ps1"
if (-not (Test-Path $configPath)) { throw "config.ps1 not found. Run:  Copy-Item config.example.ps1 config.ps1  then edit it." }
. $configPath

$ep = $ProjectEndpoint
$res = $FoundryResource
$tmp = Join-Path $PSScriptRoot "_tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Copy-Agent($srcName, $dstName) {
    Write-Output "--- Copying $srcName -> $dstName ---"
    $srcJson = az rest --method get --url "$ep/agents/$srcName`?api-version=v1" --resource $res 2>&1
    $src = $srcJson | ConvertFrom-Json
    $def = $src.versions.latest.definition
    $bodyObj = [ordered]@{
        name        = $dstName
        definition  = $def
        description = "Copy of $srcName for native REST-API Teams publish test"
    }
    $bodyPath = Join-Path $tmp "$dstName.json"
    ($bodyObj | ConvertTo-Json -Depth 40) | Set-Content -Path $bodyPath -Encoding utf8
    $out = az rest --method post --url "$ep/agents`?api-version=v1" --resource $res --headers "Content-Type=application/json" --body "@$bodyPath" 2>&1
    Write-Output $out
    Write-Output ""
}

# Clone each configured agent into its "-restapi" copy for the native-channel test.
foreach ($a in $Agents) {
    Copy-Agent $a.Agent $a.RestName
}
