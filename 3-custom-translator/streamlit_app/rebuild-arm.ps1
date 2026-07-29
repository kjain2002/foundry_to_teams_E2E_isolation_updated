<#
.SYNOPSIS
  Rebuild the ARM JSON that the Streamlit publisher deploys at runtime.

.DESCRIPTION
  The Streamlit app uses ARM REST (PUT /deployments/{name}) to deploy
  publish-agent.bicep — that requires a compiled JSON template. The Dockerfile
  does this on every container build, but for local dev (running Streamlit
  outside Docker) you need to regenerate manually whenever publish-agent.bicep
  or the policy XMLs change.

.EXAMPLE
  ./rebuild-arm.ps1
#>

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$bicepFile  = Join-Path (Split-Path $scriptRoot -Parent) 'publish-agent.bicep'
$outFile    = Join-Path $scriptRoot 'arm/publish-agent.json'

New-Item -ItemType Directory -Force -Path (Split-Path $outFile -Parent) | Out-Null

Write-Host "→ Compiling $bicepFile"
az bicep build --file $bicepFile --outfile $outFile
Write-Host "✓ Wrote $outFile ($((Get-Item $outFile).Length) bytes)"
