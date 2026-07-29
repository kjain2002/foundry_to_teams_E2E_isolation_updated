# Setup — the shared private-Foundry → Teams platform (one-time)

This is the ordered runbook to stand up the **shared** platform from scratch: one
APIM, one Container Apps environment, one router container, the two Entra apps,
and the first published agent. After this, **adding an agent = one publish** (a
registry row + Teams manifest) — no new compute.

> Concepts, limitations, and the Function-App-vs-Container comparison live in
> [DESIGN_v2_shared-router-and-preauth.md](../DESIGN_v2_shared-router-and-preauth.md). This file is just the *do-this-in-order* guide.

## Prerequisites

- A **privately-networked** Azure AI Foundry account + project (public network
  access **Disabled**), reachable over a VNet private endpoint.
- The VNet with: an APIM subnet (`/28`+), an ACA subnet (`/23`+), and a PE subnet.
- A **Key Vault** (for bot client secrets).
- `az login` as an **owner** of the subscription + tenant. **No directory-admin
  role required** (that's the whole point — see DESIGN_v2 §1).
- Tools: Azure CLI, Bicep, PowerShell 7, Python 3.11 + the `<your-python-env>` env.

---

## Step 1 — APIM bridge (public front for a private Foundry)

```powershell
az deployment group create -g <rg> `
  --template-file ../bootstrap.bicep `
  --parameters apimName=<apim-name> publisherEmail=<you@org> publisherName=<org> `
               vnetName=<vnet> apimSubnetName=<apim-subnet> foundryAccountName=<foundry>
```
Skip if your tenant already has an APIM fronting Foundry.

## Step 2 — Shared translator infra (ACR, CAE, Storage, MI, RBAC)

```powershell
az deployment group create -g <rg> `
  --template-file ../bootstrap-translator.bicep `
  --parameters vnetName=<vnet> acaSubnetName=<aca-subnet> peSubnetName=<pe-subnet> `
               foundryAccountName=<foundry> keyVaultName=<kv> uniqueSuffix=<suffix>
```
Copy its **outputs** (CAE name/domain, ACR login server, MI name/clientId, Storage
table URL) — they go into `.env` in Step 6.

## Step 3 — Entra apps (MCP resource + OAuth client + pre-authorization)

```powershell
./bootstrap_entra_apps.ps1 `
  -ResourceAppName "<your-mcp-resource-app-name>" `
  -ClientAppName   "<your-oauth-client-app-name>" `
  -RedirectUris    @("https://<apim-name>.azure-api.net/bot/api/oauth/callback")
```
This creates the scope `api://<mcp-app>/user_impersonation`, the OAuth client, and
**pre-authorizes** the client on the resource (no-admin consent). It prints the
three values (`DEFAULT_MCP_SCOPE`, `SHARED_OAUTH_CLIENT_ID`,
`SHARED_OAUTH_CLIENT_SECRET`) for Step 6. **Store the secret in Key Vault.**

## Step 4 — Build + deploy the ONE shared router container

```powershell
# Build the translator image into the shared ACR.
Push-Location ../streamlit_app/../publish-agent/translator   # translator/ with the Dockerfile
az acr build --registry <acr-name> --image translator:shared-$(Get-Date -Format yyyyMMdd-HHmmss) --file Dockerfile .
Pop-Location

# Create the shared container app (registry-driven router).
az containerapp create -g <rg> -n ca-bot-shared `
  --environment <cae-name> `
  --image <acr-login-server>/translator:shared-<tag> `
  --user-assigned <translator-mi-resource-id> `
  --ingress internal --target-port 3978 `
  --env-vars FOUNDRY_PROJECT_ENDPOINT=<foundry-project-endpoint> `
             THREAD_TABLE_URL=<storage-table-url> `
             AGENT_REGISTRY_TABLE=agents
```
> `AGENT_REGISTRY_TABLE=agents` puts the container in **shared** (read-through)
> mode. Without it the container falls back to single-agent mode — keep it set.

## Step 5 — Shared APIM routes + policies

```powershell
../setup_shared_apim_routes.ps1     # templated /agents/{agentId}/messages + /api/oauth/*
../setup_shared_apim_policies.ps1   # base policy that forwards to the shared container
```

## Step 6 — Configure `.env`

In `streamlit_app/.env` set the platform IDs from Steps 1–4 and the Entra values
from Step 3:
```dotenv
AGENT_REGISTRY_TABLE=agents
DEFAULT_MCP_SCOPE=api://<mcp-app>/user_impersonation
SHARED_OAUTH_CLIENT_ID=<client appId>
SHARED_OAUTH_CLIENT_SECRET=<client secret>       # or a Key Vault reference
OAUTH_REDIRECT_BASE_URL=https://<apim-name>.azure-api.net/bot
# DEFAULT_AUTH_MODE defaults to `app` (Foundry passthrough) — set `oauth` only
# for the bot-mints-the-token case (DESIGN_v2 §2).
```

## Step 7 — Publish the first agent

Either the notebook or the Streamlit app (same `publisher.py` under the hood):

- **Notebook:** open `../../publish-agent-via-shared-container.ipynb`, run all cells.
- **Streamlit:** `./restart.ps1` in `streamlit_app/`, then publish in the browser.

Publishing writes a registry row + Azure Bot + Teams channel + Teams `.zip`. **No
new container.**

## Step 8 — Configure the Foundry MCP tool (per agent, once)

On the agent's MCP tool in Foundry, set **OAuth Identity Passthrough → Custom**
with the Step 3 values:
- Client ID = `SHARED_OAUTH_CLIENT_ID`
- Token/Authorize/Refresh URLs = `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/{token|authorize|token}`
- Scope = `DEFAULT_MCP_SCOPE`
- Set the tool to **auto-approve** so Foundry doesn't stall on an approval request.

## Step 9 — Test in Teams

Upload the Teams `.zip`, message the agent. Expect a **"Sign in required → Open
consent"** card → Foundry consent page → **Allow access** → re-send → the agent
answers **as you**. That is Boundary A proven end to end.

---

## Tenant differences — my no-admin path vs a regular / admin tenant

I built this in a locked-down tenant with **no directory-admin role**, so a few
steps use a substitute. On a normal tenant with an admin, swap them out — **the
architecture is identical**, only the *consent + distribution* mechanics change.

| Concern | What I did (no-admin tenant) | On a regular / admin tenant |
|---|---|---|
| **Consent to the MCP scope** | **Pre-authorization** — app owner adds the client to the resource app's *Authorized client applications* (Step 3). No prompt, no admin. | Click **Grant admin consent** on the scope once (Cloud App Admin). Same org-wide effect; pre-auth becomes optional. |
| **MCP tool auth in Foundry** | OAuth Identity Passthrough → **Custom** (my own `<your-oauth-client>` client + secret). | Passthrough → **Managed** is also available (Foundry's own OAuth app) if your admin allows it — no client app of your own to manage. |
| **Teams distribution** | Sideload / "Submit to org" only (no Teams Admin). | A **Teams Administrator** publishes to the org app catalog / assigns via setup policies. |
| **Your MCP + data backend** | A backend that **can't accept an Entra JWT**, so the MCP reaches data via a **service account** (Boundary B stubbed). | A backend that **accepts the Entra JWT**, so the MCP calls the backend **as the user** — Boundary B works natively (add `validate-jwt` on the MCP). |
| **Container registry auth mode** | `app` (Foundry passthrough drives OAuth). | Same `app` default. Use `oauth` only if you want the *bot* to mint/forward the token (your existing Function App shape). |

**You supply your own everything.** Every identifier in this repo (subscription,
tenant, `<your-foundry-account>`, `<your-oauth-client>`, the MCP resource app, storage/KV names)
is **illustrative** — you create your own Entra apps, Bot Services, container, and
MCP (e.g. your delegated data MCP) in your own subscription. This repo is the **pattern
and the steps**, not shared infrastructure.

---

## Add another agent later

1. (If it needs the same MCP) reuse the shared client + scope — no new Entra work.
2. Publish (Step 7). One row, one Bot, one manifest.
3. Configure its Foundry MCP tool (Step 8).
That's it — no new container, no new APIM operation, no public endpoint.
