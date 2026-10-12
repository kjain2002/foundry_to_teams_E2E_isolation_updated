[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'deploy.config.psd1'),
    [switch]$RenderOnly,
    [switch]$SkipLink,
    # Keep existing agent sessions (they stay on the old version) instead of deleting them.
    [switch]$KeepSessions
)

$ErrorActionPreference = 'Stop'

function Assert-ConfiguredValue {
    param([hashtable]$Config, [string]$Name)

    $value = $Config[$Name]
    if ($null -eq $value -or [string]::IsNullOrWhiteSpace([string]$value) -or [string]$value -match '^<.+>$') {
        throw "Set '$Name' in $ConfigPath before deploying."
    }
}

function ConvertTo-YamlString {
    param([string]$Value)
    return "'$(($Value -replace "'", "''"))'"
}

function Invoke-CheckedCommand {
    param([string]$FilePath, [string[]]$Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

function Set-AzdValue {
    param([string]$Name, [string]$Value)
    Invoke-CheckedCommand 'azd' @('env', 'set', $Name, $Value)
}

function ConvertFrom-AzdJson {
    # azd can append an "Update available" notice; parse only the JSON body.
    param([object[]]$Lines)
    $text = ($Lines | Out-String)
    $start = $text.IndexOf('{')
    $end = $text.LastIndexOf('}')
    if ($start -lt 0 -or $end -lt $start) { return $null }
    return $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
}

function Remove-StaleSessions {
    param([string]$DeployedVersion)

    $sessions = ConvertFrom-AzdJson (azd ai agent sessions list --no-prompt 2>$null)
    if ($LASTEXITCODE -ne 0 -or $null -eq $sessions) {
        Write-Warning 'Could not list agent sessions; existing Teams chats may still run the previous version.'
        return
    }
    foreach ($session in @($sessions.data)) {
        $sessionVersion = [string]$session.version_indicator.agent_version
        if ($sessionVersion -eq $DeployedVersion) { continue }
        Write-Host "Deleting session $($session.agent_session_id) (version $sessionVersion -> $DeployedVersion)"
        azd ai agent sessions delete $session.agent_session_id --no-prompt 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Failed to delete session $($session.agent_session_id)."
        }
    }
}

# Built-in "Foundry User" role (formerly "Azure AI User"); the id is stable across renames.
$FoundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

function Grant-AgentIdentityFoundryUser {
    param([string]$PrincipalId, [string]$ProjectScope)

    if ([string]::IsNullOrWhiteSpace($PrincipalId)) {
        Write-Warning 'Agent identity principal id not found in azd ai agent show; skipping the Foundry User grant.'
        return
    }
    $existing = az role assignment list --assignee-object-id $PrincipalId --role $FoundryUserRoleId --scope $ProjectScope --query 'length(@)' --output tsv 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$existing -gt 0) {
        Write-Host "Agent identity $PrincipalId already has Foundry User on the project."
        return
    }
    az role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal `
        --role $FoundryUserRoleId --scope $ProjectScope --output none 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning ("Could not grant Foundry User to the agent identity (needs Owner or User Access Administrator). " +
            "Toolbox skills stay hidden until an admin runs:`n  az role assignment create --assignee-object-id $PrincipalId " +
            "--assignee-principal-type ServicePrincipal --role $FoundryUserRoleId --scope $ProjectScope")
        return
    }
    Write-Host "Granted Foundry User on the project to agent identity $PrincipalId (allow a few minutes to apply)."
}

function Update-TeamsPackage {
    param([string]$PackagePath, [hashtable]$Config)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $temporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("teams-app-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null

    try {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($PackagePath, $temporaryDirectory)
        $manifestPath = Join-Path $temporaryDirectory 'manifest.json'
        $manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
        $manifest.name.short = $Config.TeamsAppShortName
        $manifest.name.full = $Config.TeamsAppFullName
        $manifest.version = $Config.TeamsAppVersion
        $manifest | ConvertTo-Json -Depth 100 | Set-Content -Encoding UTF8 $manifestPath

        Remove-Item $PackagePath -Force
        Compress-Archive -Path (Join-Path $temporaryDirectory '*') -DestinationPath $PackagePath -Force
    }
    finally {
        Remove-Item $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath. Copy deploy.config.example.psd1 and fill every required value."
}

$config = Import-PowerShellDataFile $ConfigPath
$requiredValues = @(
    'TenantId', 'SubscriptionId', 'SubscriptionName', 'AzureLocation', 'ResourceGroupName',
    'FoundryAccountName', 'FoundryProjectName', 'FoundryProjectResourceId',
    'FoundryProjectEndpoint', 'ModelDeploymentName', 'AzdEnvironmentName', 'AgentName',
    'AgentDisplayName', 'BotName', 'TeamsAppShortName', 'TeamsAppFullName',
    'TeamsAppVersion', 'ServiceSourcePath', 'Runtime', 'EntryPoint', 'Cpu', 'Memory',
    'AgentDescription'
)
$requiredValues | ForEach-Object { Assert-ConfiguredValue $config $_ }

if ($config.TeamsAppShortName.Length -gt 30) {
    throw "TeamsAppShortName is $($config.TeamsAppShortName.Length) characters; Teams permits at most 30."
}
if ($config.TeamsAppFullName.Length -gt 100) {
    throw "TeamsAppFullName is $($config.TeamsAppFullName.Length) characters; Teams permits at most 100."
}

$sourcePath = Join-Path $PSScriptRoot $config.ServiceSourcePath
if (-not (Test-Path (Join-Path $sourcePath $config.EntryPoint))) {
    throw "Entry point not found: $(Join-Path $sourcePath $config.EntryPoint)"
}

# Refresh the linked target-agent definition before packaging, so the container
# always ships the current prompt + tool schemas of the configured target agent.
if (-not $SkipLink -and -not $RenderOnly -and -not [string]::IsNullOrWhiteSpace([string]$config.TargetAgentName)) {
    & (Join-Path $PSScriptRoot 'link-target-agent.ps1') -ConfigPath $ConfigPath
    if ($LASTEXITCODE -ne 0) { throw 'Target-agent linking failed; aborting deploy.' }
}

$serviceEnvironmentPrefix = ($config.AgentName.ToUpperInvariant() -replace '[^A-Z0-9]', '_')
# Optional App Insights export: the value itself lives only in the azd env (set below).
# Shipped as AGENT_APPINSIGHTS_CONNECTION_STRING because Foundry overrides the
# standard APPLICATIONINSIGHTS_CONNECTION_STRING inside hosted containers;
# main.py passes it to the host explicitly.
$useAppInsights = -not [string]::IsNullOrWhiteSpace([string]$config.AppInsightsName)
$appInsightsEnvLine = if ($useAppInsights) {
    "`n            AGENT_APPINSIGHTS_CONNECTION_STRING: `${APPLICATIONINSIGHTS_CONNECTION_STRING}"
} else { '' }
$yaml = @"
# yaml-language-server: `$schema=https://raw.githubusercontent.com/Azure/azure-dev/main/schemas/v1.0/azure.yaml.json

requiredVersions:
    azd: '>=1.27.1'
    extensions:
        azure.ai.agents: '>=1.0.0-beta.9'
name: $(ConvertTo-YamlString $config.AgentName)
services:
    $(ConvertTo-YamlString $config.AgentName):
        project: $(ConvertTo-YamlString $config.ServiceSourcePath)
        host: azure.ai.agent
        language: python
        uses:
            - ai-project
        env:
            AZURE_AI_MODEL_DEPLOYMENT_NAME: `${AZURE_AI_MODEL_DEPLOYMENT_NAME=$(ConvertTo-YamlString $config.ModelDeploymentName)}$appInsightsEnvLine
        agentEndpoint:
            authorizationSchemes:
                - type: BotServiceRbac
            protocols:
                - activity
        codeConfiguration:
            entryPoint: $(ConvertTo-YamlString $config.EntryPoint)
            runtime: $(ConvertTo-YamlString $config.Runtime)
        container:
            resources:
                cpu: $(ConvertTo-YamlString $config.Cpu)
                memory: $(ConvertTo-YamlString $config.Memory)
        description: $(ConvertTo-YamlString $config.AgentDescription)
        displayName: $(ConvertTo-YamlString $config.AgentDisplayName)
        kind: hosted
        metadata:
            tags:
                - AI Agent Hosting
                - Activity Protocol
                - Bring Your Own
                - GitHub Copilot SDK
                - Python
        name: $(ConvertTo-YamlString $config.AgentName)
        protocols:
            - protocol: activity
              version: 2.0.0
    ai-project:
        host: azure.ai.project
        deployments: []
        endpoint: $(ConvertTo-YamlString $config.FoundryProjectEndpoint)
infra:
    provider: microsoft.foundry
"@
$azureYamlPath = Join-Path $PSScriptRoot 'azure.yaml'
Set-Content -Path $azureYamlPath -Value $yaml -Encoding UTF8
Write-Host "Rendered $azureYamlPath from $ConfigPath"

if ($RenderOnly) {
    return
}

$account = az account show --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw 'Azure CLI is not signed in. Sign in manually to the configured tenant, then rerun this script.'
}
if ($account.tenantId -ne $config.TenantId -or $account.id -ne $config.SubscriptionId) {
    throw "Azure CLI context is '$($account.user.name)' / '$($account.tenantId)' / '$($account.id)'. Sign in manually and select tenant '$($config.TenantId)' and subscription '$($config.SubscriptionId)'."
}

Invoke-CheckedCommand 'az' @('account', 'set', '--subscription', $config.SubscriptionId)
$project = az resource show --ids $config.FoundryProjectResourceId --api-version '2026-09-15-preview' --output json | ConvertFrom-Json
$projectLeafName = ($project.name -split '/')[-1]
if ($LASTEXITCODE -ne 0 -or $projectLeafName -ne $config.FoundryProjectName) {
    throw "The configured Foundry project is not visible to '$($account.user.name)': $($config.FoundryProjectResourceId)"
}

$environmentNames = azd env list --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to list azd environments.'
}
$environmentExists = @($environmentNames | ForEach-Object { $_.Name }) -contains $config.AzdEnvironmentName
if ($environmentExists) {
    Invoke-CheckedCommand 'azd' @('env', 'select', $config.AzdEnvironmentName)
}
else {
    Invoke-CheckedCommand 'azd' @('env', 'new', $config.AzdEnvironmentName)
}

Set-AzdValue 'AZURE_TENANT_ID' $config.TenantId
Set-AzdValue 'AZURE_SUBSCRIPTION_ID' $config.SubscriptionId
Set-AzdValue 'AZURE_LOCATION' $config.AzureLocation
Set-AzdValue 'AZURE_RESOURCE_GROUP' $config.ResourceGroupName
Set-AzdValue 'AZURE_AI_ACCOUNT_NAME' $config.FoundryAccountName
Set-AzdValue 'AZURE_AI_PROJECT_NAME' $config.FoundryProjectName
Set-AzdValue 'AZURE_AI_PROJECT_ID' $config.FoundryProjectResourceId
Set-AzdValue 'AZURE_AI_PROJECT_ENDPOINT' $config.FoundryProjectEndpoint
Set-AzdValue 'FOUNDRY_PROJECT_ENDPOINT' $config.FoundryProjectEndpoint
Set-AzdValue 'AZURE_AI_MODEL_DEPLOYMENT_NAME' $config.ModelDeploymentName
Set-AzdValue "AGENT_${serviceEnvironmentPrefix}_BOT_NAME" $config.BotName
if ($useAppInsights) {
    $appInsightsConnection = az monitor app-insights component show --app $config.AppInsightsName --resource-group $config.ResourceGroupName --query connectionString --output tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($appInsightsConnection)) {
        throw "App Insights '$($config.AppInsightsName)' not found in '$($config.ResourceGroupName)'. Create it or set AppInsightsName = ''."
    }
    Set-AzdValue 'APPLICATIONINSIGHTS_CONNECTION_STRING' $appInsightsConnection
}

$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
try {
    Invoke-CheckedCommand 'azd' @('deploy', '--no-prompt')
    $agentShow = ConvertFrom-AzdJson (azd ai agent show --output json --no-prompt 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $agentShow.version) {
        throw 'Unable to read the deployed agent version (azd ai agent show).'
    }
    Write-Host "Deployed agent version: $($agentShow.version)"
}
finally {
    Remove-Item Env:AZURE_DEV_USER_AGENT -ErrorAction SilentlyContinue
}

# Existing sessions stay pinned to the agent version they were created on, so a
# Teams chat keeps running old code after a deploy. Delete every session not on
# the new version; the next message in each chat starts a fresh session on
# @latest. This wipes those sessions' $HOME (tasks, cached files, chat history).
if (-not $KeepSessions) {
    Remove-StaleSessions ([string]$agentShow.version)
}

# The agent identity needs Foundry User on the project to READ toolbox skills:
# without it the toolbox answers skills/list with an empty list (no error), even
# though its tools still work. Idempotent; needs role-assignment rights.
if ($config.GrantAgentIdentityFoundryUser -ne $false) {
    Grant-AgentIdentityFoundryUser ([string]$agentShow.instance_identity.principal_id) $config.FoundryProjectResourceId
}

$packagePath = Join-Path $sourcePath 'appPackage.zip'
if (-not (Test-Path $packagePath)) {
    throw "Deployment completed without generating the Teams package: $packagePath"
}
Update-TeamsPackage $packagePath $config

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($packagePath)
try {
    $manifestEntry = $archive.GetEntry('manifest.json')
    $reader = New-Object System.IO.StreamReader($manifestEntry.Open())
    try { $teamsManifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
}
finally {
    $archive.Dispose()
}
if ($teamsManifest.name.short -ne $config.TeamsAppShortName -or $teamsManifest.name.full -ne $config.TeamsAppFullName) {
    throw 'Teams package verification failed: configured names were not written to manifest.json.'
}

Write-Host "Deployment verified. Teams short name: $($teamsManifest.name.short)"
Write-Host "Teams full name: $($teamsManifest.name.full)"
Write-Host "Teams package: $packagePath"