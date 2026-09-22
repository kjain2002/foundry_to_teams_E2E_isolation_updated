$ErrorActionPreference = "Stop"
# Load shared config (copy config.example.ps1 -> config.ps1 and fill it in).
$configPath = Join-Path $PSScriptRoot "config.ps1"
if (-not (Test-Path $configPath)) { throw "config.ps1 not found. Run:  Copy-Item config.example.ps1 config.ps1  then edit it." }
. $configPath

$ep = $ProjectEndpoint
$res = $FoundryResource
$rgId = $BotServicesRgId
$tmp = Join-Path $PSScriptRoot "_tmp"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

function Publish-Agent($agentName, $display, $botName, $short, $full) {
    Write-Output "=== Publishing $agentName (scope=Shared) ==="
    $body = [ordered]@{
        agentDisplayName    = $display
        botServiceArmId     = "$rgId/$botName"
        publishScope        = "Shared"
        publishAsAutopilot  = $false
        appVersion          = "1.0.2"
        shortDescription    = $short
        fullDescription     = $full
        developerName       = $DeveloperName
        developerWebsiteUrl = $DeveloperWebsiteUrl
        privacyUrl          = $DeveloperPrivacyUrl
        termsOfUseUrl       = $DeveloperTermsUrl
    }
    $bp = Join-Path $tmp "publish-$agentName.json"
    ($body | ConvertTo-Json -Depth 10) | Set-Content -Path $bp -Encoding utf8
    $out = az rest --method post --url "$ep/agents/$agentName/microsoft365/publish`?api-version=v1" --resource $res --headers "Content-Type=application/json" --body "@$bp" 2>&1
    Write-Output $out
    Write-Output ""
}

foreach ($a in $Agents) {
    Publish-Agent $a.RestName $a.Display $a.BotName $a.Short $a.Full
}
