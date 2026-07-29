# Publish-to-Teams Steps (Privately-Networked Foundry) — manual walkthrough

> **⚠️ This document is superseded by [`publish-agent/`](publish-agent/README.md).**
> The Bicep + Streamlit publisher in that folder automates every step below
> (APIM bootstrap, per-bot Container App, JWT policy rewrite, Teams `.zip`
> packaging). **Use this document only as a reference** to understand what
> each Azure resource does, or to manually reproduce a single step for
> debugging. For new deployments, start at the [top-level README](README.md).
>
> What this manual flow *doesn't* cover that `publish-agent/` does:
> the translator Container App layer, the validate-jwt `<value>`-per-AppId
> requirement, and the per-bot APIM operation. See
> [publish-agent/README.md § Known gotchas](publish-agent/README.md#known-gotchas).

---

> **Audience.** Anyone (customer or internal) who has already deployed a
> network-isolated Microsoft Foundry resource (e.g. via the Bicep in this folder)
> and now wants the **Publish ▾ → Publish to Teams and Microsoft 365 Copilot**
> button in the new Foundry portal to actually work — including making the
> **"Azure Bot Services" dropdown populate** instead of saying "No Bot Services
> found".
>
> **Companion file.** Every action / decision / troubleshooting pivot for this
> setup is logged chronologically in
> [CHRONOLOGICAL_ACTION_LOG.md](CHRONOLOGICAL_ACTION_LOG.md). This README is the
> **clean, latest** version of the steps; the log is the **history**.

---

## 0. Architecture (target end state)

```
Teams / M365 Copilot
   │  (Microsoft Bot Channel Service — Microsoft-managed, public)
   ▼  POST /api/messages   (JWT: iss=api.botframework.com, aud=<Bot Client Id>)
Azure API Management (Developer or Premium tier, VNet-injected)
   │  • Public ingress, locked to AzureBotService service tag
   │  • Custom TLS cert on a domain you own
   │  • validate-jwt inbound policy: audience = Bot Client Id
   ▼  HTTPS (internal VNet)
Foundry messaging endpoint (privatelink.services.ai.azure.com)
   │
   ▼
Foundry agent (publicNetworkAccess: Disabled)
```

**Why this shape (and not a custom Python bot in App Service).** The
Publish-to-Teams button in the new Foundry portal does **not** want you to host
the bot adapter — Foundry hosts the Activity Protocol messaging endpoint itself
on `<resource>.services.ai.azure.com`. The only problem is that endpoint is
unreachable from Microsoft's Bot Channel Service when the Foundry resource is
private. APIM (or a YARP reverse proxy on App Service / ACA) terminates TLS with
your own cert, validates the Microsoft-signed Bot JWT, and forwards to the
Foundry private endpoint. Source: [Graeme Foster's Foundry-through-the-firewall
post](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/foundry-agents-and-custom-engine-agents-through-the-corporate-firewall/4502218),
plus internal design guidance.

> The earlier draft in [PRIVATE_FOUNDRY_TO_TEAMS.md](PRIVATE_FOUNDRY_TO_TEAMS.md)
> described a Custom Engine Agent pattern (Python bot on App Service calling the
> agent SDK). That is still a valid pattern, but it is **not** what the Publish
> button produces. Keep that doc as the fallback CEA option; use this README for
> the Publish-button path.

---

## 1. Team-friendliness & "no hardcoded bot id" rules (read first)

These are the cross-cutting decisions baked into every step below. Each was
flagged as a blocker for enterprise rollout.

| Concern | Decision in this guide |
|---|---|
| Publish flow ties the bot to one user | Bot identity is a **User-Assigned Managed Identity** whose **owner** is an **Entra security group** (`sg-foundry-bot-owners` or equivalent). Any member can re-publish, rotate, or hand off. |
| Hardcoded bot id in the Teams manifest blocks re-publish (Image #3) | Manifest keeps `"webApplicationInfo.id": "${AZURE_BOT_ID}"` and `"resource": "api://botid-${AZURE_BOT_ID}"` as tokens; a build script substitutes them at package time. Never commit a resolved GUID. |
| Same-tenant assumption | This guide assumes Foundry and Teams are in the same tenant. If not, see "Cross-tenant addendum" at bottom. |
| Portal-only publishing is "clickops" | Every Azure resource here is also expressed in Bicep (`apim.bicep`, `azure-bot.bicep`) so it can be redeployed via CI. Portal is documented as a verification path, not the source of truth. |

---

## 2. Prerequisites

- The Bicep in this folder (or equivalent) has been deployed, producing:
  - A Foundry account (`Microsoft.CognitiveServices/accounts`, kind `AIServices`)
    with `publicNetworkAccess = Disabled`.
  - A Foundry project + at least one **published** agent (note its `agentId`).
  - A VNet with two subnets (agent + private endpoint) and the
    `privatelink.services.ai.azure.com` private DNS zone linked.
- You can authenticate to that subscription with `az login` and have:
  - **Owner** (or Contributor + User Access Administrator) on the resource group.
  - **Application Administrator** (or Cloud Application Admin) on the tenant —
    needed to create the UAMI's federated bot relationship.
- You own a DNS zone you can add a record to (for the APIM custom domain) — e.g.
  `bot.contoso.com`.
- Quota for APIM Developer SKU (or Premium for prod) in the Foundry region.

---

## 3. Step-by-step (the README will be updated as we walk each one)

> The steps below will be filled in **one at a time** in chat. Each step lands
> here once it's verified. The chronological log captures the raw turn-by-turn
> activity including any rollbacks.

### Step 0 — Deploy the private Foundry infrastructure (one-time prereq)

Only needed if you don't already have a network-isolated Foundry resource.
Run from the `foundry_to_teams_privately_configured/` folder.

1. **Pick** a region + new resource group name (e.g. `swedencentral` /
   `<your-resource-group>`).
   > **Region tip:** Azure AI Search Standard SKU is frequently capacity-
   > constrained in some regions — check your subscription's quota.
   > If a deployment fails with `InsufficientResourcesAvailable` on the
   > Search resource, switch regions — `swedencentral`, `westus3`, and
   > `canadaeast` are generally good choices. This is a regional capacity
   > issue and is not fixable via quota request.
2. **Register** providers on the target subscription:
   `Microsoft.KeyVault`, `Microsoft.CognitiveServices`, `Microsoft.Storage`,
   `Microsoft.Search`, `Microsoft.Network`, `Microsoft.App`,
   `Microsoft.ContainerService`, `Microsoft.DocumentDB`,
   `Microsoft.ManagedIdentity`, `Microsoft.BotService`,
   `Microsoft.ApiManagement`.
3. **Create** the resource group:
   `az group create --name <rg> --location <region>`.
4. **Edit** `main.bicepparam` — at minimum set `location` to your region.
   Defaults create a new VNet (`192.168.0.0/16`), agent subnet
   (`192.168.0.0/24`, delegated to `Microsoft.App/environments`), PE subnet
   (`192.168.1.0/24`), six private DNS zones, Cosmos, Search, Storage, and a
   GPT-4.1 model deployment (30k TPM).
5. **Deploy**:
   ```powershell
   az deployment group create `
     --resource-group <rg> `
     --template-file main.bicep `
     --parameters main.bicepparam `
     --name foundry-private-<yyyymmdd-hhmm>
   ```
   Runs 15–25 minutes for everything *except* the two capability hosts
   (account + project). Capability host steps may exhibit a known phantom-
   timeout bug — see callout below.

   > **⚠️ KNOWN ISSUE — Capability host "phantom timeout" (verified
   > 2026-05-21 in `swedencentral`).**
   >
   > Bicep may report a failure on `account-caphost-<suffix>-deployment`
   > and/or the project caphost step with:
   > `"The resource provision operation did not complete within the
   > allowed timeout period"` after ~52 minutes. **This is almost always
   > a phantom** — the backend actually finished creating the resource in
   > 3–6 minutes, but the ARM async-operation polling URI never reports
   > the terminal state, so Bicep hangs until its 52-min provisioning
   > timeout fires.
   >
   > **First-response playbook (do NOT retry Bicep blindly):**
   >
   > 1. **Check what actually exists** — use the LIST endpoint, not
   >    by-name (the account caphost is auto-named
   >    `{accountName}@aml_aiagentservice`, ignoring the Bicep `name`
   >    parameter):
   >    ```powershell
   >    # Account caphost
   >    az rest --method GET --uri `
   >      "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<accountName>/capabilityHosts?api-version=2025-04-01-preview" `
   >      --query "value[].{name:name, state:properties.provisioningState}" -o table
   >
   >    # Project caphost
   >    az rest --method GET --uri `
   >      "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<accountName>/projects/<projectName>/capabilityHosts?api-version=2025-04-01-preview" `
   >      --query "value[].{name:name, state:properties.provisioningState}" -o table
   >    ```
   >
   > 2. **If `Succeeded`** — you're done. Move on to Step 6 (Verify outputs).
   >    Do not retry; the resource exists and is healthy.
   >
   > 3. **If missing** — do the **direct REST PUT workaround** (faster
   >    and more reliable than re-running Bicep). Re-running Bicep tends
   >    to hit 409 Conflict on the account caphost and phantom-timeout
   >    again on the project one.
   >
   >    **Account caphost** (rarely needed — usually created on first try):
   >    ```powershell
   >    @{ properties = @{
   >      capabilityHostKind = "Agents"
   >      customerSubnet = "<agentSubnetResourceId>"
   >    } } | ConvertTo-Json -Depth 5 -Compress | Out-File acct-caphost-body.json -Encoding utf8 -NoNewline
   >
   >    az rest --method PUT --uri `
   >      "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<accountName>/capabilityHosts/caphostacct?api-version=2025-04-01-preview" `
   >      --body "@acct-caphost-body.json" --headers "Content-Type=application/json"
   >    ```
   >    (Backend will rename to `<accountName>@aml_aiagentservice` regardless.)
   >
   >    **Project caphost** (most common gap — the dependent step Bicep
   >    never reached if account caphost ``failed''):
   >    ```powershell
   >    # 1. Get the 3 connection names from the project
   >    az rest --method GET --uri `
   >      "https://management.azure.com/.../accounts/<accountName>/projects/<projectName>/connections?api-version=2025-04-01-preview" `
   >      --query "value[].{name:name, category:properties.category}" -o table
   >    # Expect: <acct>search (CognitiveSearch), <acct>cosmosdb (CosmosDb), <acct>st (AzureStorageAccount)
   >
   >    # 2. Build body — uses connection NAMES (strings), not ARM IDs
   >    @{ properties = @{
   >      capabilityHostKind       = "Agents"
   >      vectorStoreConnections   = @("<searchConnectionName>")
   >      storageConnections       = @("<storageConnectionName>")
   >      threadStorageConnections = @("<cosmosConnectionName>")
   >    } } | ConvertTo-Json -Depth 5 -Compress | Out-File proj-caphost-body.json -Encoding utf8 -NoNewline
   >
   >    # 3. PUT (capabilityHostKind is REQUIRED even though Bicep types reject it)
   >    az rest --method PUT --uri `
   >      "https://management.azure.com/.../accounts/<accountName>/projects/<projectName>/capabilityHosts/caphostproj?api-version=2025-04-01-preview" `
   >      --body "@proj-caphost-body.json" --headers "Content-Type=application/json"
   >    ```
   >    Typical completion: 3–6 minutes. Poll with the LIST endpoint
   >    from step 1 until `provisioningState=Succeeded`.
   >
   > **Pinned-timestamp Bicep retry** (legacy/audit-trail option only —
   > the direct PUT above is faster and more reliable):
   >
   > ```powershell
   > $originalTs = az deployment group show `
   >   --resource-group <rg> `
   >   --name <original-deployment-name> `
   >   --query "properties.parameters.deploymentTimestamp.value" -o tsv
   >
   > az deployment group create `
   >   --resource-group <rg> `
   >   --template-file main.bicep `
   >   --parameters main.bicepparam `
   >   --parameters deploymentTimestamp=$originalTs `
   >   --name foundry-private-pinned-<yyyymmdd-hhmm>
   > ```
   > Without pinning `deploymentTimestamp`, a fresh run generates a new
   > 4-char suffix from `uniqueString(rg.id + deploymentTimestamp)` and
   > builds an entirely new Foundry/Cosmos/Search/Storage stack.

6. **Verify** outputs: Foundry account name, project endpoint, VNet id, PE
   subnet id. These become inputs for Steps 1–4.

### Step 0.9 — Establish in-VNet access to the Foundry portal (Bastion + jumpbox)

> **Why this exists.** Once `publicNetworkAccess = Disabled`, the new
> Foundry portal (`ai.azure.com`) blocks browser sessions from outside the
> VNet with a *"Private network access required"* wall — even the "Go to
> Azure AI Foundry portal" button on the resource overview blade can't
> bypass it. To click **Publish ▾ → Publish to Teams** you need a browser
> session that resolves `<account>.services.ai.azure.com` to the
> Private Endpoint IP via the linked `privatelink.services.ai.azure.com`
> DNS zone. Two viable patterns: Bastion + jumpbox VM (chosen here,
> ~$30/mo) or a Point-to-Site VPN gateway (~$30/mo, cert/Entra auth setup).

1. **Add two subnets** to the existing VNet (skipping `agent-subnet`
   because it's delegated to `Microsoft.App/environments` and can't host
   VMs or Bastion):
   ```powershell
   az network vnet subnet create -g <rg> --vnet-name <your-vnet> `
     --name AzureBastionSubnet --address-prefix 192.168.3.0/26
   az network vnet subnet create -g <rg> --vnet-name <your-vnet> `
     --name jumpbox-subnet     --address-prefix 192.168.4.0/27
   ```
   `AzureBastionSubnet` is a reserved name (required) and must be `/26`
   minimum.

2. **Deploy Bastion Developer SKU** (free, browser-based access from the
   Azure portal):
   ```powershell
   az network bastion create -g <rg> --name bastion-foundry `
     --vnet-name <your-vnet> --sku Developer
   ```
   Standard SKU (~$140/mo) is required only if you need native RDP
   client, multi-VNet, or session recording.

3. **Deploy a Win11 jumpbox VM** in `jumpbox-subnet`:
   ```powershell
   $cred = Get-Credential   # secure password prompt
   az vm create -g <rg> --name vm-jumpbox-foundry `
     --image Win11-23H2-Pro --size Standard_B2s `
     --vnet-name <your-vnet> --subnet jumpbox-subnet `
     --public-ip-address "" --nsg-rule NONE `
     --admin-username $cred.UserName --admin-password $cred.GetNetworkCredential().Password
   ```
   Cost: ~$30/mo running, ~$5/mo deallocated. **Deallocate between
   tests** to minimize spend.

4. **Connect** via Azure portal → VM → Bastion → enter creds → browser
   tab opens to the Windows desktop. From there, open Edge/Chrome →
   `https://ai.azure.com` → sign in → project should now load fully.

### Step 1 — Pre-create the Azure Bot Service (fixes "No Bot Services found" dropdown)

> **Why this is Step 1.** The Publish-to-Teams dialog populates its
> "Azure Bot Services" dropdown by **listing existing bots in the
> subscription that the caller has read access to**, AND by attempting to
> auto-create one in the current RG if the caller has
> `Microsoft.BotService/botServices/write`. Your Org's developer hit the
> empty-dropdown case because (a) no Bot Service existed yet and (b) the
> Foundry RBAC roles they had did NOT grant Bot Service write. **Pre-
> creating the bot makes the dropdown populate** regardless of caller
> permissions on the RG.

For the simplest path, use a **SingleTenant Entra app** (most realistic
for an enterprise like Your Org — locks the bot identity to the
employer's tenant only). A User-Assigned Managed Identity is more
rotation-friendly long-term (no secret to manage) but adds a federated-
identity step; we use SingleTenant here for speed and document UAMI as
an optional hardening in Step 2.

```powershell
$tenantId = "<your-tenant-id>"
$appName  = "bot-<acct>-app"

# 1. Create the SingleTenant app registration (= bot identity)
$app = az ad app create --display-name $appName `
   --sign-in-audience AzureADMyOrg | ConvertFrom-Json
$appId = $app.appId

# 2. Create a 2-year client secret
$secret = az ad app credential reset --id $appId --years 2 `
   --display-name "bot-secret" | ConvertFrom-Json
$secretValue = $secret.password   # SAVE — needed for APIM validate-jwt + bot config

# 3. Create the Azure Bot Service (F0 free SKU is fine for dev/test)
az bot create --resource-group <rg> --name bot-<acct> `
   --app-type SingleTenant --appid $appId --tenant-id $tenantId `
   --location global --sku F0
```

Leave the **messaging endpoint empty** at this point — you'll set it to
`https://<apim-host>/api/messages` in Step 4 once APIM is up.

**Verification:** `az bot show -n bot-<acct> -g <rg> --query "properties.provisioningState"`
should return `Succeeded`. Refresh the Foundry portal Publish-to-Teams
dialog — the bot should now appear in the dropdown.

### Step 2 — (Optional hardening) Replace SingleTenant secret with UAMI
*(deferred — only needed for production-grade rotation; Step 1's
SingleTenant app is sufficient for the Your Org repro and demo.)*

When you do this for real:
- Create a User-Assigned Managed Identity owned by an Entra security
  group (`sg-foundry-bot-owners`) so any group member can rotate.
- Update the bot with `--app-type UserAssignedMSI --appid <UAMI client id> --tenant-id <tenant> --msi-resource-id <UAMI resource id>`.
- Drop the client secret created in Step 1.

### Step 3 — Stand up APIM with VNet integration and custom domain

> **Why APIM is mandatory.** Microsoft Learn
> ([Publish Copilot — Limitations](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot#limitations))
> explicitly states: *"Private Link | Not supported for Teams or Azure
> Bot Service integrations."* Bot Service is a public, Microsoft-managed
> service and cannot dial a Private Endpoint. APIM is the bridge: public
> ingress (so Bot Service can reach it) + VNet-injected egress (so it
> can reach the Foundry private endpoint).

1. **Add an `apim-subnet`** to the VNet (must be a dedicated subnet — no
   PEs, no delegation):
   ```powershell
   az network nsg create -g <rg> --name nsg-apim-subnet
   # See APIM control-plane port docs for the 8 required NSG rules:
   #   https://learn.microsoft.com/azure/api-management/api-management-using-with-vnet#-required-ports
   az network vnet subnet create -g <rg> --vnet-name <your-vnet> `
     --name apim-subnet --address-prefix 192.168.2.0/24 `
     --network-security-group nsg-apim-subnet
   ```

2. **Create APIM Developer SKU** (async — takes 30–45 min):
   ```powershell
   az apim create --name apim-<acct> -g <rg> --location <region> `
     --publisher-name "<org>" --publisher-email "<email>" `
     --sku-name Developer --sku-capacity 1 `
     --virtual-network External --no-wait
   ```
   Developer SKU = $50/mo, no SLA, single unit. For production, swap to
   **Premium** ($2.8k/mo, supports zone redundancy + Internal VNet mode).

3. **Attach to `apim-subnet`** once `provisioningState=Succeeded`
   (CLI quirk: `--virtual-network External` at create time only flags
   intent; the actual subnet wire-up is a separate update):
   ```powershell
   $subnetId = az network vnet subnet show -g <rg> `
     --vnet-name <your-vnet> --name apim-subnet --query id -o tsv
   az apim update -g <rg> -n apim-<acct> --set `
     virtualNetworkType=External `
     virtualNetworkConfiguration.subnetResourceId=$subnetId
   ```
   This re-deploys the APIM data plane into the subnet (~30 min).

4. **Custom domain + TLS cert** on the gateway (required because Bot
   Service refuses to dial APIM's default `.azure-api.net` host for
   compliance reasons):
   ```powershell
   az apim update -g <rg> -n apim-<acct> --set `
     hostnameConfigurations='[{"type":"Proxy","hostName":"bot.<your-zone>","keyVaultId":"<kv-cert-uri>"}]'
   ```
   Add a CNAME `bot.<your-zone>` → `apim-<acct>.azure-api.net` in your
   DNS zone.

### Step 4 — Wire the Bot messaging endpoint to APIM and APIM backend to Foundry
*(pending)*

```powershell
# After APIM + custom domain are up:
az bot update -g <rg> -n bot-<acct> --endpoint "https://bot.<your-zone>/api/messages"
```

APIM API definition + inbound policy:
- **Backend**: `https://<account>.services.ai.azure.com/api/projects/<project>/agents/{agentId}/endpoint/protocols/activityprotocol`
  (private-DNS-resolved over the VNet integration).
- **Operation**: `POST /api/messages`.
- **Inbound policy**: `<validate-jwt>` against
  `https://login.botframework.com/v1/.well-known/openidconfiguration`,
  required claim `aud = <bot app id from Step 1>`.
- **Outbound**: rewrite headers; APIM service identity holds an
  `Azure AI Developer` role on the project so it can pass
  `Authorization: Bearer <token>` to Foundry.

### Step 5 — Add the Microsoft Teams channel
*(pending)*

```powershell
az bot msteams create -g <rg> -n bot-<acct>
```

### Step 6 — Build the Teams app manifest with `${AZURE_BOT_ID}` tokens
*(pending)*

Use a Teams `manifest.json`
as a starting point. Replace the resolved bot GUID with `${AZURE_BOT_ID}`
token so the package can be re-built per environment.

### Step 7 — Validate end-to-end and roll out via Teams Admin Center
*(pending)*

1. From the jumpbox VM (Step 0.9), open Foundry portal → project → agent →
   click **Publish ▾ → Publish to Teams and Microsoft 365 Copilot**.
2. Pick the pre-created bot from the dropdown (now populated thanks to
   Step 1).
3. Upload manifest .zip to Teams (Apps → Manage your apps → Upload
   custom app).
4. Test: send a message in the new chat → expect the agent's response.
5. For tenant-wide rollout, submit the manifest to Teams Admin Center →
   Manage apps → Upload.

---

## Cross-tenant addendum (Foundry tenant ≠ Teams tenant)

If Foundry lives in tenant A and Teams lives in tenant B (common in
acquisitions, mergers, etc.):

- The Azure Bot **must** live in the Teams tenant (because the Bot Framework's
  channel attachment is tenant-scoped).
- APIM stays in the Foundry tenant (it needs VNet line-of-sight to the Foundry
  private endpoint).
- The Bot's `MicrosoftAppType` becomes `MultiTenant` (not `UserAssignedMSI`)
  and you fall back to a federated workload identity on APIM's egress, or a
  client secret kept in Key Vault.

This adds enough complexity that we keep it out of the primary path and treat
it as a follow-up if you need it.
