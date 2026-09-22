# 5 — Native REST API publish via Activity-Protocol routing (no APIM)

> Publish a private Foundry agent to Microsoft 365 / Teams using **Foundry's own
> service-managed public route** for the Activity Protocol — no APIM, no custom
> compute, no manifest sideload. Follows the Microsoft Learn quickstart exactly.

## Goal

Same outcome as [folder 4](../4-native-rest-api-APIM-routing/), but **without
APIM**. Instead of standing up your own public proxy, you flip a switch on the
agent (`enable_m365_public_endpoint = true`) and Microsoft 365 / Teams reach
the agent over Foundry's **source-IP-filtered Activity Protocol** public route.

This is the simplest working path when your Foundry account has PNA disabled
but you don't want to run APIM.

Reference — [Publish agent to Copilot via a private network — Foundry
Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network),
plus [Allow Microsoft 365 traffic to a private-network
agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-agent?tabs=portal#allow-microsoft-365-traffic-to-a-private-network-agent).

## How it flows

```
Teams  ->  Azure Bot  ->  Foundry activity-protocol public route  ->  agent
                                (service-managed, IP-filtered)
```

Foundry does the Bot Framework ⇄ agent translation itself. All project management
APIs stay behind the private endpoint; only the delivery path is exposed.

## Prerequisites

- **Azure roles**
  - **Foundry User** on the project (permission to `az rest` GET/PATCH agents).
  - **Azure Bot Service Contributor** (or Contributor / Owner) on the target
    resource group to create the bot resource.
  - **Owner** on the subscription if you also plan to enable observability
    (Step 3 creates Log Analytics + App Insights and adds a role assignment).
- An existing **private Foundry** account + project (from
  [`1-private-foundry-infra/`](../1-private-foundry-infra/)) and a Foundry agent
  already deployed in it (Responses or hosted).
- **Azure CLI** (`az`), signed in from a client that can reach the project's
  private endpoint (VPN / ExpressRoute / bastion). Steps 1, 3, 4, and 6 hit
  private project APIs.

## What's in this folder

- `config.example.ps1` — the single config file (copy to `config.ps1`, fill in).
- `bot-service.bicep` — Azure Bot + Teams channel resource template.
- `step1-get-identity.ps1` — Step 1.
- `step2-deploy-bot.ps1` — Step 2.
- `step3-enable-activity.ps1` — Step 4.
- `step4-publish.ps1` — Step 6.
- `logging/` — optional observability (Log Analytics + App Insights + KQL) so
  you can see every Teams turn as it flows through Bot Service → Foundry.

## Step Log

<details><summary><b>Step 1 — Fetch each agent's identity client id + tenant id</b> → <a href="step1-get-identity.ps1"><code>step1-get-identity.ps1</code></a></summary>

```powershell
Copy-Item config.example.ps1 config.ps1
notepad config.ps1     # fill in FoundryAccount / FoundryProject / SubscriptionId / ResourceGroup / AgentName / BotName / display metadata
./step1-get-identity.ps1
```

Reads the agent's `instance_identity.client_id` over the private endpoint and
writes it into `config.ps1` as `$AgentClientId`. Also records `$TenantId` from
`az account show`. This client_id becomes the Bot's `msaAppId` in Step 2 — **do
not create a separate Entra app**.
</details>

<details><summary><b>Step 2 — Create the Azure Bot Service resource + Teams channel</b> → <a href="step2-deploy-bot.ps1"><code>step2-deploy-bot.ps1</code></a></summary>

```powershell
./step2-deploy-bot.ps1
```

Deploys `bot-service.bicep` with the agent's activity-protocol URL as the
messaging endpoint:

```
https://<foundry-account>.services.ai.azure.com/api/projects/<project>/agents/<agent>/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview
```

The bot is created **single-tenant** (`msaAppType: SingleTenant`) and
**public-network-access disabled** on the Bot Service ARM resource (management
plane), which does NOT affect Bot Framework's ability to POST to the messaging
endpoint. Writes the resulting Bot ARM id into `config.ps1` as `$BotServiceArmId`.
</details>

<details><summary><b>Step 3 — (Optional) Configure observability before you publish</b> → <a href="logging/enable-observability.ps1"><code>logging/enable-observability.ps1</code></a></summary>

```powershell
cd logging
./enable-observability.ps1
cd ..
```

Provisions or re-uses:
- **Log Analytics workspace** (`law-<project>`) — for Bot Service channel logs.
- **Application Insights** (`appi-<project>`) — for Foundry server-side agent
  traces.
- Turns on **Bot Service** diagnostic settings → `ABSBotRequests` in the
  workspace.

Together they let you see a Teams turn end-to-end and localize failures
(bot-side vs. Foundry-side).

**Then, one manual step in the Foundry portal:**
Open your project → **Agents → Traces → Connect** → select the App Insights
above → **Auth type: Project Managed Identity**.

The wizard grants the project MI the **Monitoring Metrics Publisher** role
(`3913510d-42f4-4e42-8a64-420c390055eb`) on that App Insights so it can
publish OpenTelemetry spans without any connection string or key.

Verify with:

```powershell
$appiId = az monitor app-insights component show `
  --app $AppInsightsName -g $ResourceGroup --query id -o tsv
az role assignment list --scope $appiId `
  --query "[].{principal:principalId, principalType:principalType, role:roleDefinitionName}" -o table
```
</details>

<details><summary><b>Step 4 — Enable the Activity Protocol + auth scheme on the agent</b> → <a href="step3-enable-activity.ps1"><code>step3-enable-activity.ps1</code></a></summary>

```powershell
./step3-enable-activity.ps1
```

PATCHes the agent's endpoint (private API) to:

- Preserve `responses` + `Entra`.
- Add `activity` protocol with `enable_m365_public_endpoint = true` (this is
  the switch that opens the service-managed, IP-filtered public route Teams
  actually uses).
- Add the auth scheme matching your `$PublishScope`:
  - `Shared` (portal *Just you*) → `BotServiceRbac`
  - `Tenant` (portal *People in your organization*) → `BotServiceTenant`

The script picks the auth scheme automatically.
</details>

<details><summary><b>Step 5 — Publish to Microsoft 365 / Teams</b> → <a href="step4-publish.ps1"><code>step4-publish.ps1</code></a></summary>

```powershell
./step4-publish.ps1
```

POSTs the Microsoft 365 publish API with the Bot ARM id from Step 2 and the
publish body from `config.ps1`. Foundry compiles a Teams app manifest
server-side and pushes it into your user's M365 catalog.

Returns `teamsAppId` + `titleId`. The app appears in Teams as a *Custom app*.
</details>

<details><summary><b>Step 6 — Install and test in Teams</b></summary>

Teams → **Apps → Manage your apps** → find the app → **Add** → open a chat →
send a message.

If nothing replies:
- Check `ABSBotRequests` in Log Analytics for delivery failures.
- Check `AppRequests` / `AppDependencies` in App Insights for agent-side errors.
- Confirm `enable_m365_public_endpoint` is still `true` on the agent (Step 4
  PATCH replaces the endpoint config wholesale — re-run if you re-PATCH later).
- Cross-tenant delivery is unreliable — see **Limitations** below.
</details>

## Limitations / trade-offs

- **Same-tenant delivery only, in practice.** The Bot is registered in the
  tenant that owns the Foundry account. Users in *that* tenant's Teams get
  reliable replies. Users in a different tenant (e.g., a partner org, or your
  Corp identity when Foundry is in Non-Prod) see intermittent silence —
  Foundry's activity-protocol server behind `enable_m365_public_endpoint`
  isn't a multi-tenant service.
- **Cannot customize the Activity Protocol implementation.** Foundry owns the
  Bot Framework ⇄ agent translation. You cannot:
  - Read files uploaded in Teams (Foundry's generated manifest hard-codes
    `supportsFiles: false`)
  - Return generated files as Teams download cards
  - Show a custom loading indicator / typing animation
  - Render your own welcome message, preference cards, consent flows
  - Persist per-user state across turns other than what the Foundry Responses
    session already gives you

  If you need any of that, use [folder 6](../6-native-BYO-activity-protocol-echo/) —
  it lets you own the activity-protocol code and layer a custom Teams UX on top
  of the same Foundry agent.
- **Same publish limits as folder 4.** `microsoft365/publish` supports
  `Shared` (just you) or `Tenant` (whole tenant, admin-approved). No
  group-level rollout.

## How this differs from folder 4

Folders 4 and 5 produce **functionally the same result** (a private Foundry
agent in Teams via `microsoft365/publish`). The single difference is the Bot's
messaging endpoint:

| | Folder 4 (APIM) | Folder 5 (no APIM) |
|---|---|---|
| Bot messagingEndpoint | `https://<apim>.azure-api.net/bot/agents/.../messages` | `https://<foundry>.services.ai.azure.com/.../activityProtocol` |
| Requires APIM? | Yes | No |
| Extra bot-side auth checks | JWT + audience validated by APIM policy | None (Foundry's built-in Bot Service RBAC) |
| Best when | You already run APIM for policy / rate-limiting / etc. | Simpler setups; the recommended path today |

Everything else — `bot-service.bicep`, `enable-activity.ps1`, `publish.ps1` —
is essentially the same shape. Folder 5 is the tighter, docs-exact recipe.

## Learn more

- [Publish agent to Copilot via a private network](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network)
  — the canonical Learn walkthrough this folder mirrors.
- [Allow Microsoft 365 traffic to a private-network agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-agent?tabs=portal#allow-microsoft-365-traffic-to-a-private-network-agent)
  — the `enable_m365_public_endpoint` switch and IP-allow-list details.
- [Bot Service diagnostic settings (ABSBotRequests)](https://learn.microsoft.com/en-us/azure/bot-service/bot-service-manage-analytics)
  — the log source used by `logging/`.
