$ErrorActionPreference = "Stop"
# --- Fill in your Foundry account + project ---
$acct = "<your-foundry-account>"; $proj = "<your-project>"
$ep = "https://$acct.services.ai.azure.com/api/projects/$proj"
$res = "https://ai.azure.com"
$tmp = Join-Path $PSScriptRoot "_tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$patch = @{
    agent_endpoint = @{
        protocol_configuration = @{ responses = @{}; activity = @{} }
        authorization_schemes  = @(@{ type = "Entra" }, @{ type = "BotServiceRbac" })
    }
}
$patchPath = Join-Path $tmp "activity-patch.json"
($patch | ConvertTo-Json -Depth 10) | Set-Content -Path $patchPath -Encoding utf8

function Enable-Activity($name) {
    Write-Output "--- PATCH $name (enable activity + BotServiceRbac) ---"
    $out = az rest --method patch --url "$ep/agents/$name`?api-version=v1" --resource $res `
        --headers "Content-Type=application/merge-patch+json" "Foundry-Features=AgentEndpoints=V1Preview" `
        --body "@$patchPath" 2>&1
    $obj = $out | ConvertFrom-Json
    Write-Output ("protocols        : {0}" -f ($obj.agent_endpoint.protocols -join ", "))
    Write-Output ("auth schemes     : {0}" -f (($obj.agent_endpoint.authorization_schemes | ForEach-Object { $_.type }) -join ", "))
    Write-Output ""
}

Enable-Activity "search-agent-restapi"
Enable-Activity "data-agent-restapi"
