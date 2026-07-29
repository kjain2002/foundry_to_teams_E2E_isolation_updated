# From Scratch: Publishing a Private Foundry Agent to Teams (legacy CEA pattern)

> **⚠️ This document describes the legacy Custom Engine Agent pattern**
> (one Python bot in App Service per agent). The current solution in
> [`publish-agent/`](publish-agent/DESIGN_v2_shared-router-and-preauth.md) replaces it with a shared APIM +
> translator-container architecture that is ~10× cheaper and provisions each
> new agent in ~2 minutes. **For new deployments, start at the
> [top-level README](README.md).** Keep this document only as a fallback for
> tenants that cannot deploy APIM or Container Apps.

---

**Starting point:** Your Org has a **privately networked Foundry resource** + project + agent, accessible via the new Foundry portal. Nothing else (no Bot resource, no App Service, no Teams app).

**End state:** agent responds in Teams, with the bot ↔ Foundry hop fully private.

> ### Prerequisite: getting *into* the private Foundry portal
>
> Once `publicNetworkAccess=Disabled`, the Foundry portal's data-plane calls (agents, threads, **Publish to Teams**) are blocked from the public internet. You need network access into the Foundry VNet first:
>
> - **Prod (Your Org):** ExpressRoute or corp site-to-site VPN already peered to the Foundry VNet.
> - **Dev / POC:** a Point-to-Site VPN Gateway with Entra ID auth — see **[../P2S_VPN_DEV_ACCESS.md](../P2S_VPN_DEV_ACCESS.md)**. Required if your tenant is **passkey/FIDO2-only** (Bastion can't pass security keys through to a remote browser).
> - **Cheap alternative:** Bastion + jumpbox VM — only viable if your tenant allows password + phone MFA.

---

## High-level architecture they'll end up with

```
Teams user
   │  (Microsoft SaaS, public)
   ▼
Bot Framework channel service
   │  HTTPS → public FQDN, locked to AzureBotService service tag
   ▼
App Service (bot adapter) — VNet integrated
   │  Private DNS → Private Endpoint
   ▼
Foundry project (private) → agent → AOAI / Search / Storage (all private)
```

Two things the **"Publish to Teams"** button in Foundry will **not** build for them:

1. A VNet-integrated bot host that can reach the private Foundry endpoint.
2. The ingress lockdown (service-tag restrictions, optional WAF).

They must build those first, then use the Publish button only to wire up the Teams channel + manifest.

---

## Phase 1 — Networking & identity foundation (do this BEFORE clicking Publish)

### 1. Inventory the existing private Foundry setup

Have them confirm:

- VNet name + region of the Foundry private endpoint(s).
- Which **Private DNS zones** are linked: at minimum `privatelink.services.ai.azure.com`, plus `privatelink.openai.azure.com`, `privatelink.cognitiveservices.azure.com`, `privatelink.search.windows.net`, `privatelink.blob.core.windows.net` (whichever apply).
- The Foundry **project endpoint** URL (e.g. `https://<resource>.services.ai.azure.com/api/projects/<project>`).
- The **agent ID** of the published agent version.

### 2. Decide where the bot will run

**Recommendation: Azure App Service (Linux, Python 3.11)** — matches a Python Bot Framework SDK app and supports Regional VNet Integration cleanly. Alternatives: Container Apps with internal environment, or App Service Environment v3 if they want full isolation.

### 3. Create the subnet for VNet integration

- A **dedicated `/27` (or larger) subnet** in the Foundry VNet (or a peered VNet), delegated to `Microsoft.Web/serverFarms`.
- Cannot have other resources or delegations in it.

### 4. Create a User-Assigned Managed Identity (UAMI)

One identity to share between the Bot resource and the App Service:

```powershell
az identity create --name bot-uami --resource-group <rg> --location <region>
```

Grab the `clientId`, `principalId`, and full resource ID.

### 5. Grant the UAMI access to the Foundry project

On the Foundry project, assign the UAMI the **Azure AI User** role (or a custom equivalent). This is what lets the bot call the agent without keys.

---

## Phase 2 — Deploy the bot service

### 6. Provision the App Service Plan + Web App

- **Plan**: Linux, P1v3 or better (needed for VNet integration at scale).
- **Web App**: Python 3.11, in the same region as the Foundry VNet.

### 7. Attach the UAMI to the Web App

App Service → **Identity** → **User assigned** → add `bot-uami`.

### 8. Enable Regional VNet Integration

App Service → **Networking** → **VNet integration** → select the subnet from step 3. Then set app setting:

```
WEBSITE_VNET_ROUTE_ALL = 1
```

This forces **all** outbound traffic — including DNS lookups for the Foundry FQDN — through the VNet, where it will resolve to the private IP.

### 9. Lock down inbound

App Service → **Networking** → **Access restrictions** on the main site:

- Allow rule: **Service tag = `AzureBotService`** on `/api/messages`.
- (Optional) Allow corporate IP ranges for admin/health probes.
- Default action: **Deny**.

### 10. Deploy the bot code

Use your bot service code (a Bot Framework SDK app). Configure these app settings:

| Setting | Value |
|---|---|
| `MicrosoftAppType` | `UserAssignedMSI` |
| `MicrosoftAppId` | UAMI **clientId** |
| `MicrosoftAppTenantId` | tenant ID |
| `AZURE_EXISTING_AIPROJECT_ENDPOINT` | Foundry project URL |
| `AZURE_EXISTING_AGENT_ID` | published agent ID |
| `WEBSITE_VNET_ROUTE_ALL` | `1` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `1` |

Deploy via `az webapp deploy` / zip deploy. Confirm the site starts and `/api/messages` returns 401 unauthenticated (proves it's up).

### 11. Smoke test the private hop

From the Kudu / SSH console of the Web App:

```
nslookup <resource>.services.ai.azure.com
```

Must resolve to a **10.x.x.x** (private) address. If it resolves to a public IP, the Private DNS zone is not linked to the integration VNet — fix before continuing.

---

## Phase 3 — Use Foundry's "Publish to Teams" button

Now the prerequisites the Publish flow needs (a bot host with a real messaging endpoint) exist.

### 12. In the Foundry portal

- Open the agent → click **Publish** ▾ → **Publish to Teams and Microsoft 365 Copilot**.
- When it asks about the **Azure Bot resource**: choose **Create new** (they don't have one).
  - Subscription / RG: same as the App Service.
  - Pricing tier: F0 for dev, S1 for prod.
  - **Identity**: User-Assigned Managed Identity → pick `bot-uami` (same one the Web App uses). App type = `UserAssignedMSI`.
- When it asks for **messaging endpoint**: enter the existing App Service URL:

  ```
  https://<app-service-name>.azurewebsites.net/api/messages
  ```

  **Do not** let it provision a new App Service / adapter. If the flow insists on creating one, cancel and instead create the Azure Bot resource manually (Portal → Azure Bot → set messaging endpoint + UAMI identity), then return to the Foundry Publish flow and select **Use existing**.

### 13. Add the Microsoft Teams channel

In the Publish wizard (or on the Bot resource → **Channels**), add **Microsoft Teams**. Accept the terms.

### 14. Download the Teams app manifest

The Publish flow produces a zipped Teams app package (`manifest.json` + color + outline icons). Save it.

---

## Phase 4 — Roll out in Teams

### 15. Sideload for pilot

Teams → **Apps** → **Manage your apps** → **Upload a custom app** → pick the zip. Test in a 1:1 chat with the bot.

### 16. Org-wide deployment

Teams Admin Center → **Teams apps** → **Manage apps** → **Upload new app** → upload zip. Then create / edit a **Permission policy** + **Setup policy** to make it available (or pin it) for the target users.

---

## Phase 5 — Validate end-to-end

- Message the bot in Teams → response within a few seconds.
- App Service logs (`az webapp log tail`) show:
  - Incoming POST from Bot Framework on `/api/messages`.
  - Outbound call to `<resource>.services.ai.azure.com` succeeding.
- Foundry portal → agent → **Threads / Traces**: shows the conversation arriving via the bot's UAMI.
- Network watcher: confirm no traffic egressing to public Foundry IPs from the App Service subnet.

---

## What the security team will ask — pre-answer it

| Concern | Answer |
|---|---|
| Is the bot host public? | The web app's outbound is fully private (VNet-integrated). Inbound is public **but** restricted to Microsoft's `AzureBotService` service tag + JWT auth — required by the Teams channel; no first-party private path exists for it. |
| Are credentials in plaintext? | No. UAMI is used everywhere — no app secret stored anywhere. |
| Can Foundry be reached from the public internet via the bot? | No. Bot calls Foundry only through the VNet via Private Endpoint. Foundry's public network access is disabled. |
| What if Foundry rotates keys / endpoints? | UAMI-based auth has no keys; endpoint changes only require an app-setting update. |
| What about Teams data residency? | Teams ↔ Bot Framework remains Microsoft-managed traffic; payload only crosses public network between Microsoft and Microsoft. Sensitive enterprise data hop (bot → Foundry → indexes) stays inside their VNet. |

---

## Order-of-operations cheat sheet

1. UAMI → Foundry RBAC
2. Subnet + Private DNS validation
3. App Service Plan + Web App + UAMI attached
4. VNet integration + `WEBSITE_VNET_ROUTE_ALL=1`
5. Inbound access restrictions (`AzureBotService` tag)
6. Deploy bot code + app settings
7. DNS / connectivity smoke test (private IP resolution)
8. **Now** click Publish to Teams in Foundry → create Bot resource → point at existing Web App
9. Add Teams channel → download manifest
10. Sideload → Teams Admin Center rollout
