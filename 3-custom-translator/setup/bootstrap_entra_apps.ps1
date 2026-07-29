<#
=============================================================================
 bootstrap_entra_apps.ps1 — create the two Entra apps the shared platform needs
=============================================================================
 Creates, idempotently, the identity objects that the OAuth Identity Passthrough
 flow depends on — the ones you'd otherwise create by hand:

   1. MCP RESOURCE app  — exposes  api://<appId>/user_impersonation  (the scope
      that becomes the token 'audience'). This is what the MCP protects.

   2. OAuTH CLIENT app  — the app Foundry (Custom passthrough) uses to run the
      OBO. It is PRE-AUTHORIZED on the resource for user_impersonation, which is
      the no-admin substitute for admin consent (see DESIGN_v2 §1).

 It also: sets the identifier URI, creates service principals for both apps,
 grants the client delegated access to the resource scope, and mints a client
 secret. At the end it prints the exact values to paste into streamlit_app/.env.

 Requires: az CLI logged in (`az login`) as an OWNER of the target tenant. NO
 directory-admin role is needed — creating app registrations and pre-authorizing
 a client you own are owner-level actions.

 Usage:
   ./bootstrap_entra_apps.ps1 `
       -ResourceAppName "<your-mcp-resource-app-name>" `
       -ClientAppName   "<your-oauth-client-app-name>" `
       -RedirectUris    @("https://<your-apim-name>.azure-api.net/bot/api/oauth/callback")
=============================================================================
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]   $ResourceAppName,
    [Parameter(Mandatory)] [string]   $ClientAppName,
    [Parameter(Mandatory)] [string[]] $RedirectUris,
    [string] $ScopeName = "user_impersonation"
)

$ErrorActionPreference = "Stop"
$graph = "https://graph.microsoft.com/v1.0"

function Graph($method, $url, $bodyObj = $null) {
    if ($null -ne $bodyObj) {
        $tmp = New-TemporaryFile
        ($bodyObj | ConvertTo-Json -Depth 20) | Set-Content -Path $tmp -Encoding utf8
        try   { return az rest --method $method --url $url --headers "Content-Type=application/json" --body "@$tmp" 2>$null | ConvertFrom-Json }
        finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
    return az rest --method $method --url $url 2>$null | ConvertFrom-Json
}

function Get-AppByName($name) {
    $enc = [uri]::EscapeDataString("displayName eq '$name'")
    $resp = Graph GET "$graph/applications?`$filter=$enc&`$select=id,appId,identifierUris,api"
    if ($resp.value.Count -gt 0) { return $resp.value[0] }
    return $null
}

function Ensure-Sp($appId) {
    $enc  = [uri]::EscapeDataString("appId eq '$appId'")
    $resp = Graph GET "$graph/servicePrincipals?`$filter=$enc&`$select=id"
    if ($resp.value.Count -eq 0) {
        Graph POST "$graph/servicePrincipals" @{ appId = $appId } | Out-Null
        Write-Host "  created service principal for $appId" -ForegroundColor DarkGray
    }
}

# ── 1. Resource app ────────────────────────────────────────────────────────
Write-Host "1. MCP resource app: $ResourceAppName" -ForegroundColor Cyan
$res = Get-AppByName $ResourceAppName
if (-not $res) {
    $res = Graph POST "$graph/applications" @{ displayName = $ResourceAppName; signInAudience = "AzureADMyOrg" }
    Write-Host "  created appId $($res.appId)" -ForegroundColor Green
} else {
    Write-Host "  reusing appId $($res.appId)" -ForegroundColor Green
}
$resourceObjectId = $res.id
$resourceAppId    = $res.appId
$identifierUri    = "api://$resourceAppId"

# Reuse an existing scope id if the scope already exists, else mint one.
$scopeId = $null
if ($res.api -and $res.api.oauth2PermissionScopes) {
    $existing = $res.api.oauth2PermissionScopes | Where-Object { $_.value -eq $ScopeName } | Select-Object -First 1
    if ($existing) { $scopeId = $existing.id }
}
if (-not $scopeId) { $scopeId = [guid]::NewGuid().ToString() }

# ── 2. Client app (references the scope id we now know) ────────────────────
Write-Host "2. OAuth client app: $ClientAppName" -ForegroundColor Cyan
$client = Get-AppByName $ClientAppName
$clientBody = @{
    displayName    = $ClientAppName
    signInAudience = "AzureADMyOrg"
    web            = @{ redirectUris = $RedirectUris }
    requiredResourceAccess = @(
        @{
            resourceAppId  = $resourceAppId
            resourceAccess = @(@{ id = $scopeId; type = "Scope" })
        }
    )
}
if (-not $client) {
    $client = Graph POST "$graph/applications" $clientBody
    Write-Host "  created appId $($client.appId)" -ForegroundColor Green
} else {
    Graph PATCH "$graph/applications/$($client.id)" $clientBody | Out-Null
    Write-Host "  reusing appId $($client.appId) (redirects + permission refreshed)" -ForegroundColor Green
}
$clientObjectId = $client.id
$clientAppId    = $client.appId

# ── 3. Resource: expose the scope + pre-authorize the client (one PATCH) ───
Write-Host "3. Expose scope + pre-authorize client on the resource" -ForegroundColor Cyan
$resourcePatch = @{
    identifierUris = @($identifierUri)
    api = @{
        oauth2PermissionScopes = @(
            @{
                id                      = $scopeId
                value                   = $ScopeName
                type                    = "User"
                isEnabled               = $true
                adminConsentDisplayName = "Access $ResourceAppName as the signed-in user"
                adminConsentDescription = "Allow the app to call $ResourceAppName on behalf of the signed-in user."
                userConsentDisplayName  = "Access $ResourceAppName on your behalf"
                userConsentDescription  = "Allow the app to call $ResourceAppName as you."
            }
        )
        # PRE-AUTHORIZATION — the no-admin consent substitute.
        preAuthorizedApplications = @(
            @{ appId = $clientAppId; delegatedPermissionIds = @($scopeId) }
        )
    }
}
Graph PATCH "$graph/applications/$resourceObjectId" $resourcePatch | Out-Null
Write-Host "  scope api://$resourceAppId/$ScopeName exposed; client pre-authorized" -ForegroundColor Green

# ── 4. Service principals ──────────────────────────────────────────────────
Write-Host "4. Service principals" -ForegroundColor Cyan
Ensure-Sp $resourceAppId
Ensure-Sp $clientAppId

# ── 5. Client secret ───────────────────────────────────────────────────────
Write-Host "5. Client secret (for the OAuth client)" -ForegroundColor Cyan
$secretResp = Graph POST "$graph/applications/$clientObjectId/addPassword" `
    @{ passwordCredential = @{ displayName = "bootstrap-$(Get-Date -Format yyyyMMdd)" } }
$clientSecret = $secretResp.secretText

# ── Output ─────────────────────────────────────────────────────────────────
Write-Host "`n=== DONE — paste these into streamlit_app/.env ===" -ForegroundColor Yellow
Write-Host "DEFAULT_MCP_SCOPE=api://$resourceAppId/$ScopeName"
Write-Host "SHARED_OAUTH_CLIENT_ID=$clientAppId"
Write-Host "SHARED_OAUTH_CLIENT_SECRET=$clientSecret"
Write-Host "`n(Also set the SAME three on the Foundry MCP tool: OAuth Identity Passthrough -> Custom.)" -ForegroundColor DarkGray
Write-Host "SECURITY: the secret is shown ONCE. Store it in Key Vault; rotate if it leaks." -ForegroundColor Red
