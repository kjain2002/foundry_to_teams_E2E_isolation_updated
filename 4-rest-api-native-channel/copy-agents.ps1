$ErrorActionPreference = "Stop"
# --- Fill in your Foundry account + project ---
$acct = "<your-foundry-account>"; $proj = "<your-project>"
$ep = "https://$acct.services.ai.azure.com/api/projects/$proj"
$res = "https://ai.azure.com"
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

# Examples: clone your existing agents into "-restapi" copies used for the native-channel test.
Copy-Agent "search-agent" "search-agent-restapi"
Copy-Agent "data-agent" "data-agent-restapi"
