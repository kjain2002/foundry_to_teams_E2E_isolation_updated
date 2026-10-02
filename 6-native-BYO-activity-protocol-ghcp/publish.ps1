# ============================================================================
# Publish the deployed hosted agent to Microsoft 365 / Teams.
#
# Calls the Foundry Microsoft 365 publish API
#   POST {projectEndpoint}/agents/{agentName}/microsoft365/publish?api-version=v1
# so the Teams app is compiled and submitted to the M365/Teams catalog without
# manually sideloading appPackage.zip or clicking Publish in the portal.
#
# TeamsDisplayName (agentDisplayName in the request body) is the name shown in
# Teams/M365. All values come from deploy.config.psd1.
# ============================================================================
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'deploy.config.psd1')
)

$ErrorActionPreference = 'Stop'

function Assert-ConfiguredValue {
    param([hashtable]$Config, [string]$Name)

    $value = $Config[$Name]
    if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value) -or [string]$value -match '^<.+>$') {
        throw "Set '$Name' in $ConfigPath before publishing."
    }
}

if (-not (Test-Path $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath."
}

$config = Import-PowerShellDataFile $ConfigPath
$requiredValues = @(
    'TenantId', 'SubscriptionId', 'ResourceGroupName', 'FoundryProjectEndpoint',
    'AgentName', 'BotName', 'TeamsDisplayName', 'PublishScope', 'PublishAppVersion',
    'ShortDescription', 'FullDescription', 'DeveloperName', 'DeveloperWebsiteUrl',
    'PrivacyUrl', 'TermsOfUseUrl'
)
$requiredValues | ForEach-Object { Assert-ConfiguredValue $config $_ }

# Guard the signed-in identity so we never publish from the wrong tenant/subscription.
$account = az account show --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw 'Azure CLI is not signed in. Sign in to the configured tenant, then rerun.'
}
if ($account.tenantId -ne $config.TenantId -or $account.id -ne $config.SubscriptionId) {
    throw "Azure CLI context is '$($account.user.name)' / tenant '$($account.tenantId)' / sub '$($account.id)'. Select tenant '$($config.TenantId)' and subscription '$($config.SubscriptionId)' first."
}

# Resolve the Azure Bot created during deploy and confirm it exists.
$botResourceId = "/subscriptions/$($config.SubscriptionId)/resourceGroups/$($config.ResourceGroupName)/providers/Microsoft.BotService/botServices/$($config.BotName)"
$bot = az resource show --ids $botResourceId --output json 2>$null | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $bot) {
    throw "Azure Bot '$($config.BotName)' was not found at $botResourceId. Deploy the agent first (deploy.ps1)."
}

$body = [ordered]@{
    agentDisplayName    = $config.TeamsDisplayName
    botServiceArmId     = $botResourceId
    publishScope        = $config.PublishScope
    publishAsAutopilot  = $false
    appVersion          = $config.PublishAppVersion
    shortDescription    = $config.ShortDescription
    fullDescription     = $config.FullDescription
    developerName       = $config.DeveloperName
    developerWebsiteUrl = $config.DeveloperWebsiteUrl
    privacyUrl          = $config.PrivacyUrl
    termsOfUseUrl       = $config.TermsOfUseUrl
} | ConvertTo-Json -Depth 10

$bodyFile = New-TemporaryFile
Set-Content -Path $bodyFile -Value $body -Encoding utf8 -NoNewline

$publishUrl = "$($config.FoundryProjectEndpoint)/agents/$($config.AgentName)/microsoft365/publish?api-version=v1"
Write-Host "== Publishing '$($config.AgentName)' to Microsoft 365 (scope=$($config.PublishScope)) ==" -ForegroundColor Cyan
Write-Host "Teams display name: $($config.TeamsDisplayName)"
Write-Host $body

try {
    $response = az rest --method post --url $publishUrl --resource 'https://ai.azure.com' `
        --headers 'Content-Type=application/json' --body "@$bodyFile"
    if ($LASTEXITCODE -ne 0) {
        throw "Publish request failed ($LASTEXITCODE). See the error above."
    }
}
finally {
    Remove-Item $bodyFile -Force -ErrorAction SilentlyContinue
}

Write-Host "`nPublish response:" -ForegroundColor Green
Write-Host $response

$parsed = $null
try { $parsed = $response | ConvertFrom-Json } catch { $parsed = $null }
if ($parsed) {
    Write-Host ''
    if ($parsed.titleId) { Write-Host "titleId    = $($parsed.titleId)" -ForegroundColor Green }
    if ($parsed.teamsAppId) { Write-Host "teamsAppId = $($parsed.teamsAppId)" -ForegroundColor Green }
    Write-Host "`nOpen Microsoft Teams -> Apps -> Built for your org (or 'Your agents') and find '$($config.TeamsDisplayName)'."
}
