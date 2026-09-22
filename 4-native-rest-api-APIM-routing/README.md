# 4 — Native REST API publish via APIM routing

> Publish a private Foundry agent to Microsoft 365 / Teams using **APIM as the
> public bot messaging endpoint** in front of Foundry's activity-protocol route.
> All plain REST calls, no custom container.

## Goal

Get a private-network Foundry agent into Teams by:
1. Enabling Foundry's built-in `activityProtocol` on the agent.
2. Fronting that private endpoint with an **APIM operation** that Bot Framework
   can reach publicly.
3. Registering an Azure Bot whose messaging endpoint is the APIM URL, then
   publishing to Microsoft 365 via Foundry's `microsoft365/publish` API.

This is the same recipe as folder 5, **except** the bot's messaging endpoint is
the APIM URL instead of Foundry's activity-protocol URL directly. Use this when
your Foundry account has PNA disabled and you *cannot* rely on Foundry's
service-managed `enable_m365_public_endpoint` route from folder 5.

Reference — [Foundry Agents and Custom Engine Agents through the Corporate
Firewall (Microsoft Community Hub)](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/foundry-agents-and-custom-engine-agents-through-the-corporate-firewall/4502218).

## How it flows

```
Teams  ->  Azure Bot  ->  APIM public URL  ->  Foundry activity-protocol (private endpoint)
```

Foundry does the Bot Framework ⇄ agent translation itself; APIM just proxies
inbound `/agents/{id}/messages` to the matching Foundry route and validates the
Bot Framework JWT.

## Prerequisites

- **Azure roles**
  - **Owner** (or Contributor + User Access Administrator) on the target subscription — the deploy creates the Bot Service and reads/writes APIM config.
  - **Foundry User** (or higher) on the project so `az rest` can read/patch agents.
  - **API Management Contributor** on the APIM instance to update named values + policy.
- An existing **private Foundry** account + project (from
  [`1-private-foundry-infra/`](../1-private-foundry-infra/)).
- An existing **APIM** instance in front of that Foundry (deployed as part of
  folder 1 or separately).
- **Azure CLI** (`az`), signed in to a client that can reach the project's
  private endpoint (VPN / ExpressRoute / bastion), because Steps 3 and 6 hit
  private project APIs.

## What's in this folder

- `config.example.ps1` — the single config file (copy to `config.ps1`, fill in your
  values). Every script loops over the `$Agents` array so you only edit this file.
- `bot-service.bicep` — Azure Bot + Teams channel resource template (deployed
  once per agent by `deploy-bots.ps1`).
- `apim-api-policy.original.xml` — untouched copy of your APIM API-level policy,
  captured before any edits.
- `copy-agents.ps1` — **Step 2**, clone each Responses agent into a `-restapi` copy
  (skip if you're publishing the agent as-is).
- `enable-activity.ps1` — **Step 3**, PATCH each agent to enable the `activity`
  protocol + `Entra` and `BotServiceRbac` auth schemes.
- `deploy-bots.ps1` — **Step 4**, deploy `bot-service.bicep` per agent (bot +
  Teams channel).
- `wire-apim.ps1` — **Step 5**, add an APIM operation that rewrites
  `/agents/{id}/messages` to Foundry's activity-protocol route + merges each
  `BotAppId` into the API's `validate-jwt` audience list.
- `publish.ps1` — **Step 6**, call Foundry's `microsoft365/publish` API per agent.
- `manifest-cifiles/` — sample Teams manifest that a user in the *same tenant*
  as the Bot could sideload for the CI Files agent (illustrative — the
  `microsoft365/publish` flow doesn't require you to build/upload the manifest).

## Step Log

<details><summary><b>Step 0 — Fetch each agent's identity client id (the <code>BotAppId</code>)</b></summary>

```powershell
az rest --method get `
  --url "$ProjectEndpoint/agents/<your-agent>?api-version=v1" `
  --resource https://ai.azure.com `
  --query instance_identity.client_id -o tsv
```

The bot's `msaAppId` **must** be the agent's own `instance_identity.client_id`.
**Do NOT create a separate Entra app** — Bot Framework won't accept replies from
Foundry if the ids don't match. Paste each value into `$Agents[].BotAppId`.
</details>

<details><summary><b>Step 1 — Configure once: copy the example and fill it in</b></summary>

```powershell
Copy-Item config.example.ps1 config.ps1   # config.ps1 is git-ignored
notepad config.ps1
```

Fill in:
- `$FoundryAccount`, `$FoundryProject`, `$SubscriptionId`, `$ResourceGroup`, `$TenantId`
- `$ApimName`, `$ApimApiName` (your APIM + API)
- `$Agents` — one row per agent (`Agent`, `RestName`, `BotName`, `BotAppId`,
  `Display`, `Short`, `Full`). Every script below loops over `$Agents`.
</details>

<details><summary><b>Step 2 — Clone each Responses agent into a <code>-restapi</code> copy</b> → <a href="copy-agents.ps1"><code>copy-agents.ps1</code></a></summary>

```powershell
./copy-agents.ps1
```

Reads each `$Agents[].Agent` and creates the `RestName` copy for the native-channel test.
Skip (set `RestName = Agent`) if you want to publish your existing agent directly.
</details>

<details><summary><b>Step 3 — Enable the activity protocol + auth schemes on each agent</b> → <a href="enable-activity.ps1"><code>enable-activity.ps1</code></a></summary>

```powershell
./enable-activity.ps1
```

PATCHes each agent's endpoint to add the `activity` protocol and the
`Entra` + `BotServiceRbac` auth schemes. Runs `az rest` against a project
private-endpoint URL, so you need tunnel/VPN connectivity.
</details>

<details><summary><b>Step 4 — Create the Azure Bot + Teams channel per agent</b> → <a href="deploy-bots.ps1"><code>deploy-bots.ps1</code></a></summary>

```powershell
./deploy-bots.ps1
```

Deploys `bot-service.bicep` per agent. The messaging endpoint is set to the
**APIM URL** (`https://<apim>.azure-api.net/bot/agents/<agent>/messages`), not
Foundry's activity-protocol URL directly.
</details>

<details><summary><b>Step 5 — Wire APIM: add the routing operation + merge JWT audiences</b> → <a href="wire-apim.ps1"><code>wire-apim.ps1</code></a></summary>

```powershell
./wire-apim.ps1
```

- Adds an APIM operation `POST /agents/{id}/messages` that rewrites to Foundry's
  `activityProtocol` endpoint on the private VNet.
- Merges each `BotAppId` into the API-level `validate-jwt` `allowed-bot-audiences`
  named value so APIM accepts Bot Framework tokens minted for any of your bots.
- The original policy is preserved in `apim-api-policy.original.xml`.
</details>

<details><summary><b>Step 6 — Publish to Microsoft 365 / Teams</b> → <a href="publish.ps1"><code>publish.ps1</code></a></summary>

```powershell
./publish.ps1
```

Calls Foundry's `microsoft365/publish` API for each agent. Foundry generates
the Teams manifest server-side and pushes the app into your user's M365 catalog
(scope = `Shared`).

Returns `teamsAppId` + `titleId` per agent — the app then appears in
Teams → **Apps → Manage your apps** as a *Custom app*.
</details>

<details><summary><b>Step 7 — Install in Teams and test</b></summary>

Open the agent in Teams (Apps → Manage your apps → find it → **Add**), send a
message, confirm it replies. Live path:

```
Teams  ->  Azure Bot  ->  APIM  ->  Foundry activity-protocol  ->  agent
```

If nothing replies:
- Verify APIM's `allowed-bot-audiences` includes the agent's `BotAppId`.
- Verify the Bot messagingEndpoint is the APIM URL (not the raw Foundry URL).
- Cross-tenant delivery is unreliable — see **Limitations** below.
</details>

## Limitations / trade-offs

- **Message delivery is only reliable *same-tenant*.** The Bot is single-tenant
  and Foundry's private endpoint sits in one tenant. Users in that tenant's
  Teams see replies deterministically. Users in a different tenant may or may
  not receive replies — Bot Framework's cross-tenant delivery to the APIM URL
  works, but Foundry's activity-protocol server behind APIM is single-tenant.
- **No customizable Teams UX.** Foundry generates the Teams manifest (fixed:
  `supportsFiles: false`). You cannot:
  - Read files the user uploads in Teams (no `supportsFiles`)
  - Return files as a proper Teams download card
  - Customize the loading indicator, welcome message, adaptive cards
  - Add preference collection UI
- **Publish scope is user or tenant only** — no group-level rollout via the
  Foundry publish API (that would require a Teams admin uploading a custom
  package + assigning it via a Setup Policy).
- **Requires APIM.** If you don't need the extra hop for policy reasons, use
  the simpler [folder 5](../5-native-rest-api-activity-protocol-routing/) —
  it removes APIM entirely.

Need file support or custom Teams UX? See [folder 6](../6-native-BYO-activity-protocol-echo/).

## Learn more

- [Foundry Agents and Custom Engine Agents through the Corporate Firewall](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/foundry-agents-and-custom-engine-agents-through-the-corporate-firewall/4502218)
  — the design pattern this folder implements.
- [Publish to Copilot from a virtual network — Foundry Agents](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network)
  — the underlying `microsoft365/publish` API contract.
