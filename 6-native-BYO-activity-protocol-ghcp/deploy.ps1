[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'deploy.config.psd1'),
    [switch]$RenderOnly
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

$serviceEnvironmentPrefix = ($config.AgentName.ToUpperInvariant() -replace '[^A-Z0-9]', '_')
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
            AZURE_AI_MODEL_DEPLOYMENT_NAME: `${AZURE_AI_MODEL_DEPLOYMENT_NAME=$(ConvertTo-YamlString $config.ModelDeploymentName)}
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

$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
try {
    Invoke-CheckedCommand 'azd' @('deploy', '--no-prompt')
    Invoke-CheckedCommand 'azd' @('ai', 'agent', 'show', '--output', 'json')
}
finally {
    Remove-Item Env:AZURE_DEV_USER_AGENT -ErrorAction SilentlyContinue
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