# ============================================================================
# Step 4: Publish the agent to Microsoft 365
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
#
# POST the Microsoft 365 publish API. The agent name is in the URL; the service
# resolves the agent + identity from it. Returns a titleId on success.
# NOTE: This calls a project API protected by the private endpoint.
# ============================================================================
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/config.ps1"

if (-not $BotServiceArmId) { throw "BotServiceArmId is empty. Run ./step2-deploy-bot.ps1 first." }

$body = @{
    agentDisplayName    = $AgentDisplayName
    botServiceArmId     = $BotServiceArmId
    publishScope        = $PublishScope
    publishAsAutopilot  = $false
    appVersion          = $AppVersion
    shortDescription    = $ShortDescription
    fullDescription     = $FullDescription
    developerName       = $DeveloperName
    developerWebsiteUrl = $DeveloperWebsiteUrl
    privacyUrl          = $PrivacyUrl
    termsOfUseUrl       = $TermsOfUseUrl
} | ConvertTo-Json -Depth 10

$tmp = New-TemporaryFile
Set-Content -Path $tmp -Value $body -NoNewline

$url = "$ProjectEndpoint/agents/$AgentName/microsoft365/publish?api-version=v1"
Write-Host "== Step 4: POST Microsoft 365 publish ($PublishScope) ==" -ForegroundColor Cyan
Write-Host $body

az rest --method post --url $url --resource $FoundryResource `
    --headers "Content-Type=application/json" `
    --body "@$tmp"

Remove-Item $tmp -Force
Write-Host "`nPublished. Check the response above for titleId." -ForegroundColor Green
Write-Host "Verify in Microsoft 365 Copilot / Teams store (Shared => 'Your agents')."
