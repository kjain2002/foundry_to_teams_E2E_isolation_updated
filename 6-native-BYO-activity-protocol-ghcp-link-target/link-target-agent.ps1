# ============================================================================
# Link a target agent into this helper (single agent/session).
#
# Runs fetch_target_agent.py to extract the target agent's prompt + tool schemas
# from Foundry and write src/activity-agent/target-agent.yaml. Handles the
# Python dependencies for you in a short-path venv (the project path is long
# enough to trip Windows MAX_PATH for native wheels, so the venv lives under
# %LOCALAPPDATA%, not in the repo).
#
# Usage:
#   ./link-target-agent.ps1                      # uses TargetAgentName from deploy.config.psd1
#   ./link-target-agent.ps1 -Agent my-agent      # override the target
# deploy.ps1 calls this automatically when TargetAgentName is set.
# ============================================================================
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'deploy.config.psd1'),
    [string]$Agent,
    [string]$Endpoint
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $ConfigPath)) {
    throw "Configuration file not found: $ConfigPath."
}
$config = Import-PowerShellDataFile $ConfigPath

$agentName = if ($Agent) { $Agent } else { [string]$config.TargetAgentName }
$endpoint = if ($Endpoint) { $Endpoint } else { [string]$config.FoundryProjectEndpoint }
$outDir = Join-Path $PSScriptRoot $config.ServiceSourcePath

if ([string]::IsNullOrWhiteSpace($agentName)) {
    Write-Host "TargetAgentName is empty - nothing to link; the helper keeps its own persona." -ForegroundColor Yellow
    return
}

# Short-path venv so native wheels (cffi/cryptography) load under MAX_PATH.
$venvRoot = Join-Path $env:LOCALAPPDATA 'ghcp-linktool'
$venvPython = Join-Path $venvRoot 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating link-tool venv at $venvRoot ..." -ForegroundColor Cyan
    & python -m venv (Join-Path $venvRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python venv. Is python on PATH?' }
    & $venvPython -m pip install --quiet --disable-pip-version-check httpx pyyaml azure-identity
    if ($LASTEXITCODE -ne 0) { throw 'Could not install link-tool dependencies.' }
}

$fetchScript = Join-Path $PSScriptRoot 'fetch_target_agent.py'
Write-Host "== Linking target agent '$agentName' ==" -ForegroundColor Cyan
& $venvPython $fetchScript --agent $agentName --endpoint $endpoint --out-dir $outDir
if ($LASTEXITCODE -ne 0) { throw "Linking failed. See the error above." }
Write-Host "Linked. target-agent.yaml refreshed in $outDir" -ForegroundColor Green
