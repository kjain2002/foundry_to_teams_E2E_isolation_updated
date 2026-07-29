<#
.SYNOPSIS
    Short-lived test of the Terraform Foundry module: validate -> plan ->
    apply -> verify -> DESTROY. The destroy runs in a finally block, so the
    resources are torn down even if a step fails (cost-safety guarantee).

.DESCRIPTION
    Creates the project + App Insights (+ Log Analytics) + an optional model
    deployment against an EXISTING Foundry account, prints the outputs, verifies
    the project via the Azure CLI, then destroys everything this run created.
    The existing account is only READ (a data source) and is never deleted.

    Run from anywhere; the script locates the module folder relative to itself.

.PARAMETER VarFile
    Path to the tfvars to use. Defaults to ..\test.tfvars next to the module.
    Copy test.tfvars.example -> test.tfvars and edit the account name + RG first.

.PARAMETER SkipDestroy
    Leave the resources in place after the run (you must destroy them yourself).
    Use only when you want to inspect the result; remember it costs money.

.PARAMETER AutoApprove
    Skip the interactive confirmation before apply. Destroy is always automatic.

.EXAMPLE
    .\Test-And-Destroy.ps1
    .\Test-And-Destroy.ps1 -VarFile ..\test.tfvars -AutoApprove
#>
[CmdletBinding()]
param(
    [string] $VarFile = (Join-Path $PSScriptRoot '..\test.tfvars'),
    [switch] $SkipDestroy,
    [switch] $AutoApprove
)

$ErrorActionPreference = 'Stop'

# Module dir = the parent of this script's folder (test\ lives inside the module).
$ModuleDir = Resolve-Path (Join-Path $PSScriptRoot '..')

function Assert-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "'$name' not found on PATH. Open a fresh shell or install it, then retry."
    }
}

Assert-Tool terraform
Assert-Tool az

if (-not (Test-Path $VarFile)) {
    throw "Var file '$VarFile' not found. Copy test.tfvars.example -> test.tfvars and edit the account name + resource group."
}
$VarFile = (Resolve-Path $VarFile).Path

# Confirm an Azure login exists (apply needs it; destroy needs it too).
try { az account show 1>$null 2>$null } catch { }
if ($LASTEXITCODE -ne 0) { throw "Not logged in to Azure. Run 'az login' (and 'az account set --subscription <id>') first." }

Push-Location $ModuleDir
$applied = $false
try {
    Write-Host "`n=== init ===" -ForegroundColor Cyan
    terraform init -input=false
    if ($LASTEXITCODE -ne 0) { throw 'terraform init failed.' }

    Write-Host "`n=== validate ===" -ForegroundColor Cyan
    terraform validate
    if ($LASTEXITCODE -ne 0) { throw 'terraform validate failed.' }

    Write-Host "`n=== plan ===" -ForegroundColor Cyan
    terraform plan -input=false -var-file="$VarFile"
    if ($LASTEXITCODE -ne 0) { throw 'terraform plan failed.' }

    if (-not $AutoApprove) {
        $ans = Read-Host "`nProceed to APPLY these resources? They will be DESTROYED at the end. (y/N)"
        if ($ans -notin @('y', 'Y')) { Write-Host 'Aborted before apply. Nothing was created.' -ForegroundColor Yellow; return }
    }

    Write-Host "`n=== apply ===" -ForegroundColor Cyan
    terraform apply -auto-approve -input=false -var-file="$VarFile"
    if ($LASTEXITCODE -ne 0) { throw 'terraform apply failed.' }
    $applied = $true

    Write-Host "`n=== outputs ===" -ForegroundColor Green
    terraform output

    # Verify what was created via the Azure CLI (project + model deployment).
    Write-Host "`n=== verify (az) ===" -ForegroundColor Green
    $proj   = (terraform output -raw project_name) 2>$null
    $acct   = (terraform output -raw account_name) 2>$null
    $rg     = (terraform output -raw account_resource_group) 2>$null
    $model  = (terraform output -raw model_deployment_name) 2>$null

    if ($rg -and $acct -and $proj) {
        Write-Host "Project '$proj' on account '$acct' (rg '$rg'):" -ForegroundColor Green
        az cognitiveservices account project show --name $acct -g $rg --project-name $proj `
            --query "{name:name, location:location, provisioningState:properties.provisioningState}" -o table

        Write-Host "`nModel deployments on account '$acct':" -ForegroundColor Green
        az cognitiveservices account deployment list --name $acct -g $rg `
            --query "[].{name:name, model:properties.model.name, version:properties.model.version, sku:sku.name, capacity:sku.capacity}" -o table

        if ($model) { Write-Host "`nExpected model deployment from this run: $model" -ForegroundColor Green }
    }
    else {
        Write-Host "Created project: $proj (raw-ID path - skipping az show; no resource group output)." -ForegroundColor Yellow
    }
}
finally {
    if ($applied -and -not $SkipDestroy) {
        Write-Host "`n=== destroy (cleanup) ===" -ForegroundColor Magenta
        terraform destroy -auto-approve -input=false -var-file="$VarFile"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "DESTROY FAILED - resources may still exist. Run manually:`n  cd `"$ModuleDir`"; terraform destroy -var-file=`"$VarFile`""
        }
        else {
            Write-Host "Cleanup complete - all test resources destroyed (the existing account is untouched)." -ForegroundColor Magenta
        }
    }
    elseif ($SkipDestroy -and $applied) {
        Write-Warning "SkipDestroy set - resources LEFT IN PLACE (they cost money). Destroy with:`n  cd `"$ModuleDir`"; terraform destroy -var-file=`"$VarFile`""
    }
    Pop-Location
}
