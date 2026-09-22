# ============================================================================
# Step 3: Enable source IP-filtered Activity Protocol access + Bot Service auth
# https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network
#
# PATCH the agent to set protocol_configuration and authorization_schemes.
# This REPLACES both collections, so we keep `responses` + `Entra` and ADD
# `activity` (enable_m365_public_endpoint=true) + BotServiceRbac.
#
# BotServiceRbac pairs with publishScope=Shared. If you publish Tenant scope,
# change the scheme below to BotServiceTenant to avoid it being replaced later.
# NOTE: This calls a project API protected by the private endpoint.
# ============================================================================
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/config.ps1"

$authScheme = if ($PublishScope -eq 'Tenant') { 'BotServiceTenant' } else { 'BotServiceRbac' }

$body = @{
    agent_endpoint = @{
        protocol_configuration = @{
            responses = @{}
            activity  = @{ enable_m365_public_endpoint = $true }
        }
        authorization_schemes = @(
            @{ type = 'Entra' }
            @{ type = $authScheme }
        )
    }
} | ConvertTo-Json -Depth 10

$tmp = New-TemporaryFile
Set-Content -Path $tmp -Value $body -NoNewline

$url = "$ProjectEndpoint/agents/$AgentName`?api-version=v1"
Write-Host "== Step 3: PATCH agent endpoint (activity + $authScheme) ==" -ForegroundColor Cyan
Write-Host $body

az rest --method patch --url $url --resource $FoundryResource `
    --headers "Content-Type=application/merge-patch+json" `
    --body "@$tmp"

Remove-Item $tmp -Force
Write-Host "`nActivity protocol enabled with $authScheme." -ForegroundColor Green
Write-Host "Next: ./step4-publish.ps1"
