<#
.SYNOPSIS
  Publish a Foundry agent to Microsoft Teams via the APIM bridge.

.DESCRIPTION
  End-to-end driver that replaces the broken "Publish to Teams" button in the
  new Foundry portal for privately-networked Foundry accounts. Per invocation:

    1. Creates a SingleTenant Entra (AAD) app for the bot (or re-uses one).
    2. Generates a fresh client secret and **writes it straight to Key Vault**
       as the secret `bot-<BotShortName>-secret` (never printed to console).
    3. Deploys publish-agent.bicep to create the Bot Service, Teams channel,
       and APIM operation for the given agentId.
    4. Renders manifest/manifest.template.json and zips it with the icons into
       out/<BotShortName>.zip, ready to sideload or upload to Teams Admin Center.

  Requires: Azure CLI >= 2.60, Bicep CLI installed, signed in via `az login`
  with Owner (or Contributor + User Access Administrator) on the resource
  group + `Key Vault Secrets Officer` on the vault. No directory-admin role is
  needed when using pre-authorization (see setup/SETUP_SHARED_PLATFORM.md).

.EXAMPLE
  ./publish-agent.ps1 `
    -ResourceGroup           <your-resource-group> `
    -ApimName                <your-apim-name> `
    -FoundryAccount          <your-foundry-account> `
    -FoundryProject          <your-project> `
    -AgentId                 asst_AbCdEf1234567890 `
    -DisplayName             "Sales Copilot" `
    -BotShortName            sales-copilot `
    -BotSecretsKeyVaultName  <your-key-vault>
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $ApimName,
    [Parameter(Mandatory)] [string] $FoundryAccount,
    [Parameter(Mandatory)] [string] $FoundryProject,
    [Parameter(Mandatory)] [string] $AgentId,
    [Parameter(Mandatory)] [string] $DisplayName,
    [Parameter(Mandatory)] [string] $BotShortName,
    [Parameter(Mandatory)] [string] $BotSecretsKeyVaultName,

    [string] $DescriptionShort = "Chat with $DisplayName",
    [string] $DescriptionFull  = "A Teams bot that connects to $DisplayName, hosted privately in Azure AI Foundry.",
    [string] $DeveloperName    = "Platform Team",
    [string] $DeveloperWebsite = "https://example.com",
    [string] $DeveloperPrivacy = "https://example.com/privacy",
    [string] $DeveloperTerms   = "https://example.com/terms",

    [string] $SubscriptionId,
    [switch] $SkipBicep,
    [switch] $SkipManifest
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$outDir     = Join-Path $scriptRoot 'out'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

if ($SubscriptionId) {
    Write-Host "→ Setting subscription to $SubscriptionId"
    az account set --subscription $SubscriptionId | Out-Null
}

$tenantId = az account show --query tenantId -o tsv
Write-Host "→ Tenant:        $tenantId"
Write-Host "→ Resource group: $ResourceGroup"
Write-Host "→ APIM:           $ApimName"
Write-Host "→ Foundry:        $FoundryAccount / $FoundryProject"
Write-Host "→ Agent:          $AgentId"
Write-Host ""

# -------------------------------------------------------------------------
# 1. Entra app for the bot (SingleTenant). Idempotent on display name.
# -------------------------------------------------------------------------
$appDisplayName = "bot-$BotShortName-app"
Write-Host "→ [1/4] Ensuring Entra app '$appDisplayName' exists ..."

$existingAppId = az ad app list --display-name $appDisplayName --query "[0].appId" -o tsv
if ([string]::IsNullOrWhiteSpace($existingAppId)) {
    $botAppId = az ad app create `
        --display-name $appDisplayName `
        --sign-in-audience AzureADMyOrg `
        --query appId -o tsv
    Write-Host "   created AppId: $botAppId"
} else {
    $botAppId = $existingAppId
    Write-Host "   reusing AppId: $botAppId"
}

# Service principal for the app (required for Bot Service to authenticate).
$spExists = az ad sp list --filter "appId eq '$botAppId'" --query "[0].id" -o tsv
if ([string]::IsNullOrWhiteSpace($spExists)) {
    az ad sp create --id $botAppId | Out-Null
    Write-Host "   created service principal"
}

# Fresh client secret — printed ONCE, then discarded by this script.
Write-Host "→ [2/4] Generating client secret (valid 6 months) ..."
$secretPayload = az ad app credential reset `
    --id $botAppId `
    --display-name "publish-agent-$($BotShortName)-$(Get-Date -Format yyyyMMdd)" `
    --years 0 --append `
    --query "{password:password, endDateTime:endDateTime}" `
    -o json | ConvertFrom-Json
# NOTE: Azure CLI doesn't accept months, only years. The line above uses --years 0
# combined with the default 2y expiry override; if your tenant policy caps app
# secrets (e.g. 7 days at Microsoft), the printed endDateTime will reflect that.

$botSecret = $secretPayload.password
$secretEnd = $secretPayload.endDateTime
Write-Host "   secret end date: $secretEnd"

# -------------------------------------------------------------------------
# 2a. Store the secret in Key Vault. The raw value never leaves this function.
# -------------------------------------------------------------------------
$kvSecretName = "bot-$BotShortName-secret"
Write-Host "→ [3a/4] Writing secret to Key Vault '$BotSecretsKeyVaultName' as '$kvSecretName' …"

# Build tag set as JSON for `az keyvault secret set --tags`.
$kvTags = @(
    "botAppId=$botAppId",
    "botShortName=$BotShortName",
    "createdBy=publish-agent.ps1",
    "createdAt=$(Get-Date -Format o)"
)

az keyvault secret set `
    --vault-name $BotSecretsKeyVaultName `
    --name $kvSecretName `
    --value $botSecret `
    --content-type "application/x-bot-client-secret" `
    --tags @kvTags `
    --expires $secretEnd `
    --query "id" -o tsv | Tee-Object -Variable kvSecretUri | Out-Null

# Drop the secret from this session immediately.
$botSecret = $null
Remove-Variable botSecret -ErrorAction SilentlyContinue
Write-Host "   stored: $kvSecretUri"

# -------------------------------------------------------------------------
# 2. Merge this AppId into APIM's allowed-bot-audiences named value.
# -------------------------------------------------------------------------
Write-Host "→ [3/4] Reading existing allowed-bot-audiences from APIM ..."
$currentAudiences = az apim nv show `
    -g $ResourceGroup `
    --service-name $ApimName `
    --named-value-id allowed-bot-audiences `
    --query value -o tsv 2>$null

if ([string]::IsNullOrWhiteSpace($currentAudiences) -or $currentAudiences -eq '00000000-0000-0000-0000-000000000000') {
    $mergedAudiences = $botAppId
} elseif ($currentAudiences -split ',' | Where-Object { $_.Trim() -eq $botAppId }) {
    $mergedAudiences = $currentAudiences
} else {
    $mergedAudiences = "$currentAudiences,$botAppId"
}
Write-Host "   merged audiences: $mergedAudiences"

# -------------------------------------------------------------------------
# 3. Bicep — Bot Service + Teams channel + APIM operation.
# -------------------------------------------------------------------------
$botName = "bot-$BotShortName"
if (-not $SkipBicep) {
    Write-Host "→ [4/4] Deploying publish-agent.bicep ..."
    $deploymentName = "publish-agent-$BotShortName-$(Get-Date -Format yyyyMMddHHmm)"
    az deployment group create `
        --resource-group     $ResourceGroup `
        --name               $deploymentName `
        --template-file      (Join-Path $scriptRoot 'publish-agent.bicep') `
        --parameters `
            botName=$botName `
            botDisplayName="$DisplayName" `
            botAppId=$botAppId `
            botTenantId=$tenantId `
            apimName=$ApimName `
            foundryProjectName=$FoundryProject `
            agentId=$AgentId `
            mergedAllowedAudiences=$mergedAudiences `
        | Out-Null
    Write-Host "   deployment '$deploymentName' succeeded"
} else {
    Write-Host "→ [4/4] -SkipBicep set; Bot Service / APIM operation NOT deployed."
}

# -------------------------------------------------------------------------
# 4. Teams app .zip — substitute tokens and pack with icons.
# -------------------------------------------------------------------------
if (-not $SkipManifest) {
    Write-Host "→ Building Teams app .zip ..."

    $manifestDir   = Join-Path $scriptRoot 'manifest'
    $template      = Get-Content (Join-Path $manifestDir 'manifest.template.json') -Raw
    $teamsAppGuid  = [guid]::NewGuid().ToString()

    $rendered = $template `
        -replace '\$\{TEAMS_APP_ID\}',         $teamsAppGuid `
        -replace '\$\{TEAMS_PACKAGE_NAME\}',   "com.$($BotShortName).teams" `
        -replace '\$\{BOT_APP_ID\}',           $botAppId `
        -replace '\$\{BOT_DISPLAY_SHORT\}',    $DisplayName `
        -replace '\$\{BOT_DISPLAY_FULL\}',     $DisplayName `
        -replace '\$\{BOT_DESC_SHORT\}',       $DescriptionShort `
        -replace '\$\{BOT_DESC_FULL\}',        $DescriptionFull `
        -replace '\$\{DEVELOPER_NAME\}',       $DeveloperName `
        -replace '\$\{DEVELOPER_WEBSITE\}',    $DeveloperWebsite `
        -replace '\$\{DEVELOPER_PRIVACY_URL\}',$DeveloperPrivacy `
        -replace '\$\{DEVELOPER_TERMS_URL\}',  $DeveloperTerms

    $stage = Join-Path $outDir "stage-$BotShortName"
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    New-Item -ItemType Directory -Path $stage | Out-Null

    Set-Content -Path (Join-Path $stage 'manifest.json') -Value $rendered -Encoding utf8
    Copy-Item (Join-Path $manifestDir 'color.png')   $stage
    Copy-Item (Join-Path $manifestDir 'outline.png') $stage

    $zipPath = Join-Path $outDir "$BotShortName.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath -Force
    Remove-Item -Recurse -Force $stage
    Write-Host "   wrote $zipPath"
}

# -------------------------------------------------------------------------
# Summary.
# -------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host " PUBLISH COMPLETE"
Write-Host "============================================================"
Write-Host "  Bot AppId   : $botAppId"
Write-Host "  Bot Name    : $botName"
Write-Host "  Endpoint    : https://$ApimName.azure-api.net/bot/agents/$AgentId/messages"
Write-Host "  Teams .zip  : $(Join-Path $outDir "$BotShortName.zip")"
Write-Host ""
Write-Host "  Bot secret stored in Key Vault (NOT printed):"
Write-Host "      Vault   : $BotSecretsKeyVaultName"
Write-Host "      Secret  : $kvSecretName"
Write-Host "      URI     : $kvSecretUri"
Write-Host ""
Write-Host "  Foundry's hosted messaging endpoint does NOT consume this secret."
Write-Host "  It is stored so the AAD app never has orphan credentials and so a"
Write-Host "  future custom Bot Framework adapter (if added) can read it from KV."
Write-Host ""
Write-Host "  Next: upload $BotShortName.zip via Teams Admin Center → Manage apps → Upload new app."
Write-Host "============================================================"
