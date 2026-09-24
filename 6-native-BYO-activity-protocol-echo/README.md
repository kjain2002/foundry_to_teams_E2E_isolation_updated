# 6 — BYO Activity Protocol (hosted agent) with customizable Teams UX

> Deploy your **own** hosted agent that speaks the Foundry **Activity Protocol
> natively**, sitting as a thin front-door in front of an existing Foundry
> Responses agent. Gives you a fully-owned Teams UX (loading indicator, file
> upload/download consent flow, preference poll, durable per-user state) that
> the Foundry-managed publish path in [folder 5](../5-native-rest-api-activity-protocol-routing/)
> cannot expose.

## Goal

Same target audience as folder 5 (Teams users) and the same target brain (an
existing Foundry Responses agent — you don't rebuild it), but you own the
activity-protocol implementation. That lets you:

- Show a **Teams typing/loading indicator** while the model works.
- Ingest **Teams file uploads** and PUT them into the target agent's session
  sandbox so its tools can read them.
- Detect **files the agent generates**, offer them as a Teams **file-consent
  card**, and upload the accepted bytes to the user's OneDrive.
- Render a **preference / onboarding card** on first contact and honor an
  ongoing conversation history + `RESET**` reset command.
- Persist all of the above in **Azure Blob** so it survives cold starts,
  redeploys, and multiple replicas.

The agent's business logic still lives in the target Foundry Responses agent —
this folder is a Teams-facing helper (a "front door"), not a replacement.

Reference — [`foundry-samples/samples/python/hosted-agents/bring-your-own/activity/`](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity)
(`echo` for the minimal skeleton, `github-copilot` for the fuller pattern).

## How it flows

```
Teams user
   |  (Bot messagingEndpoint = this hosted agent's activity URL)
   v
agent-activity  (this folder, hosted by Foundry)
   |  responses.create(x-ms-user-token=<user OBO>, agent_session_id=<sid>)
   v
your existing Foundry Responses agent (unchanged)
   |
   +--> Foundry Toolbox / MCP tools (Starburst, etc.)
   +--> Code Interpreter / Files (in the same session sandbox this helper uses)
```

Every Teams turn hits the helper. It:
1. Applies UX (typing indicator, welcome card, consent card).
2. Ingests attachments into the target agent's session sandbox.
3. Forwards the message to the existing Responses agent, passing
   `x-ms-user-token` and `agent_session_id`.
4. Streams the reply back to Teams and delivers any generated files via the
   Teams FileConsent → OneDrive flow.

## Prerequisites

- **Azure roles**
  - **Owner** (or Contributor + User Access Administrator) on the target
    subscription — the `azd` deploy creates a hosted agent + role assignments.
  - **Foundry User** or **Azure AI User** on the project.
  - **Azure Bot Service Contributor** (or higher) on the target RG.
  - **Storage Blob Data Contributor** on the storage account you use for
    durable state — granted **to the agent's managed identity** (Step 6).
- **Azure Developer CLI** (`azd`) 1.34+ with the `azure.ai.agents` extension:

  ```powershell
  azd extension install azure.ai.agents
  ```
- **Docker** (optional — `azd deploy` uses a remote ACR build by default).
- **Python 3.13** for local testing.
- An **existing target Foundry agent** (Responses protocol) — folder 6 doesn't
  ship one; it forwards to whatever you point at with `TARGET_AGENT_NAME`.
- Network reachability to the project's private endpoint for `azd deploy`
  and for the follow-up `az rest` PATCH.

## What's in this folder

- `azure.yaml` — azd manifest declaring the hosted agent + which existing
  Foundry project to bind (`ai-project.endpoint`).
- `bot-service.bicep` — Azure Bot + Teams channel; endpoint = the helper's
  activity-protocol URL after Step 3.
- `build_package.py` — generates a sideload-ready Teams app package with
  **`supportsFiles: true`** pointing at the same Bot (see Step 8).
- `teams-app-package/manifest.json` + icons — the manifest the build script
  produces (regenerated on each build).
- `.env.example` — env vars consumed by both local runs and `azd deploy`.
- `.azure/` — azd environment directory (per-user, git-ignored).
- `src/agent-activity/` — the helper's code:
  - `main.py` — `ActivityAgentServerHost` + `@app.activity("message" / "invoke" / "conversationUpdate")` handlers, orchestrates typing/attachment ingest/file delivery/prefs.
  - `foundry_bridge.py` — thin `AIProjectClient` bridge that forwards each turn to the target Responses agent with `x-ms-user-token`.
  - `sessions.py` — session sandbox API (`ensure_session`, upload/list/download files).
  - `attachments.py` — Teams file-download-info parser.
  - `outfiles.py` — `FileConsentCard` + `fileConsent/invoke` handler that PUTs bytes to the OneDrive upload URL and posts a `FileInfoCard`.
  - `prefs.py` — the onboarding poll + conversation history (backed by `state_store`).
  - `state_store.py` — Azure Blob-backed durable state (managed identity, no keys).
  - `token_claims.py` — safe-decode of the user OBO token for observability.
  - `config.py`, `Dockerfile`, `requirements.txt`.

## Step Log

<details><summary><b>Step 1 — Scaffold: copy env, initialize azd</b></summary>

> [!NOTE]
> Pure local setup — nothing is deployed to Azure yet.
> You copy the example env file and create a named `azd` environment (a folder
> that stores your settings), then tell it *which existing Foundry project to
> target*. That binding is what stops the next step from spinning up a brand-new
> Foundry account instead of reusing yours.

```powershell
Copy-Item .env.example .env    # fill in FOUNDRY_PROJECT_ENDPOINT, TARGET_AGENT_NAME, MCP_USER_SCOPE, STATE_STORAGE_ACCOUNT
azd env new <env-name>         # e.g. agent-activity-dev
```

Then edit `.azure/<env-name>/.env` and set at minimum:

```
AZURE_SUBSCRIPTION_ID="<your-subscription-id>"
AZURE_TENANT_ID="<your-tenant-id>"
AZURE_LOCATION="<your-region>"                   # match the Foundry project's region
AZURE_RESOURCE_GROUP="<your-resource-group>"
AZURE_AI_ACCOUNT_NAME="<your-foundry-account>"
AZURE_AI_PROJECT_NAME="<your-project>"
AZURE_AI_PROJECT_ID="/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>"
USE_EXISTING_AI_PROJECT="true"
ENABLE_HOSTED_AGENTS="true"
ENABLE_CAPABILITY_HOST="false"
FOUNDRY_PROJECT_ENDPOINT="https://<foundry>.services.ai.azure.com/api/projects/<project>"
TARGET_AGENT_NAME="<your-existing-agent-name>"
STATE_STORAGE_ACCOUNT="<your-storage-account>"
MCP_USER_SCOPE=""                                # e.g. api://<mcp-app-id>/user_impersonation
OAUTH_CONNECTION_NAME=""                         # optional Bot OAuth connection name
AZURE_AI_MODEL_DEPLOYMENT_NAME="gpt-5"
```

**Key gotcha:** the `USE_EXISTING_AI_PROJECT="true"` line **plus** the
`ai-project.endpoint` in `azure.yaml` are what bind the deploy to your
*existing* Foundry account/project. Without both, azd will happily provision
a **new** account (with a name like `cog-xxxxxxxxxxxx`) alongside yours.
</details>

<details><summary><b>Step 1.5 — Grant Bot Service permission (needed before <code>azd up</code>)</b></summary>

> [!NOTE]
> `azd up` (Step 2) doesn't only register the agent — it
> also **creates an Azure Bot** so Teams can reach it. Creating that Bot needs
> write access to Bot Service on the resource group. Give yourself (or ask an
> admin to give you) the role below **before** running Step 2 so the Bot part
> succeeds. The steps below check what you have, then grant what's missing.

**1) Set your target scope**

```powershell
$SubscriptionId = "<your-subscription-id>"
$ResourceGroup  = "<your-resource-group>"      # e.g. RG-FOUNDRY-DEV-WESTUS
$Scope          = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
$MyId           = az ad signed-in-user show --query id -o tsv
```

**2) Check whether you already have it**

```powershell
# List your role assignments on the RG (Owner or Contributor both grant BotService write)
az role assignment list `
  --assignee $MyId `
  --scope $Scope `
  --include-inherited `
  --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

You are good if you see **Owner**, **Contributor**, or a custom role that
includes `Microsoft.BotService/botServices/write` (or `Microsoft.BotService/*`).
If you only see reader-type roles, you'll hit the 403 — continue to step 3.

**3) Grant the permission** (whoever runs this needs **Owner** or **User Access
Administrator** on the RG/subscription — often a subscription admin)

```powershell
# Simplest: Contributor on the RG (includes Microsoft.BotService/botServices/write)
az role assignment create `
  --assignee $MyId `
  --role "Contributor" `
  --scope $Scope
```

Least-privilege alternative — a custom role scoped to just Bot Service:

```powershell
# Define once per subscription
$roleJson = @{
  Name             = "Bot Service Writer"
  Description      = "Create/manage Azure Bot resources for hosted agents"
  Actions          = @("Microsoft.BotService/*")
  AssignableScopes = @("/subscriptions/$SubscriptionId")
} | ConvertTo-Json -Depth 5
$tmp = New-TemporaryFile ; Set-Content $tmp -Value $roleJson -NoNewline
az role definition create --role-definition "@$tmp"
Remove-Item $tmp -Force

az role assignment create --assignee $MyId --role "Bot Service Writer" --scope $Scope
```

**4) Register the resource provider** (needed once per subscription; `azd`'s
Bot creation and Step 5 both require it)

```powershell
az account set --subscription $SubscriptionId
az provider register --namespace Microsoft.BotService
az provider show --namespace Microsoft.BotService --query registrationState -o tsv   # -> Registered
```

Role assignments can take a minute or two to propagate. Re-run the Step&nbsp;2
verification (`az role assignment list` above) until your new role shows, then
proceed. If `azd up` already failed on this, just fix the role and run
`azd deploy` — the agent is already deployed, only the Bot binding is retried.
</details>

<details><summary><b>Step 2 — <code>azd up</code>: build image + register the hosted agent</b></summary>

> [!NOTE]
> This one command does three things at once, which is why
> it's easy to miss what's happening: (1) it **builds your helper code** in
> `src/agent-activity/` into a container image (using a remote ACR build),
> (2) it **registers that image as a hosted agent** called `agent-activity`
> inside your Foundry project, and (3) a `postdeploy` hook then **creates the
> Azure Bot** that Teams talks to. So `azd up` = build + register agent + create
> bot. Step 1.5 exists because that third part needs Bot Service permission.

```powershell
azd up --no-prompt
```

- Uses ACR remote build to package `src/agent-activity/` into a container.
- Registers a hosted agent named `agent-activity` in your existing project
  (protocol = activity, auth scheme = `BotServiceRbac` from `azure.yaml`).
- Prints the agent's activity-protocol URL:
  ```
  https://<foundry>.services.ai.azure.com/api/projects/<project>/agents/agent-activity/endpoint/protocols/activityProtocol
  ```

If the deploy provisions a new account instead of using the existing one, check
that `ai-project.endpoint` in `azure.yaml` points at *your* project.
</details>

<details><summary><b>Step 3 — Enable the M365 public endpoint on the helper agent</b></summary>

> [!NOTE]
> Your Foundry project sits behind a locked-down private
> network, so even after Step 2 the agent is deployed but **unreachable from
> Teams**. This step flips one switch (`enable_m365_public_endpoint`) that opens
> a Microsoft-managed public doorway *just* for Bot Framework traffic, so Teams
> messages can actually get in. Without it, the bot stays silent.

```powershell
$ProjectEndpoint = "https://<foundry>.services.ai.azure.com/api/projects/<project>"
$body = @{
  agent_endpoint = @{
    protocol_configuration = @{
      activity = @{ enable_m365_public_endpoint = $true }
    }
    authorization_schemes = @(@{ type = 'BotServiceRbac' })
  }
} | ConvertTo-Json -Depth 10
$tmp = New-TemporaryFile ; Set-Content $tmp -Value $body -NoNewline
az rest --method patch `
  --url "$ProjectEndpoint/agents/agent-activity?api-version=v1" `
  --resource https://ai.azure.com `
  --headers "Content-Type=application/merge-patch+json" `
  --body "@$tmp"
Remove-Item $tmp -Force
```

`azd deploy` sets `activity` protocol + `BotServiceRbac` but leaves
`protocol_configuration.activity` as `{}`. The private project needs the
service-managed public route to be **explicitly enabled** — this PATCH flips
`enable_m365_public_endpoint` to `true` so Bot Framework can deliver messages.

Verify:

```powershell
az rest --method get `
  --url "$ProjectEndpoint/agents/agent-activity?api-version=v1" `
  --resource https://ai.azure.com `
  --query agent_endpoint.protocol_configuration.activity
```

Should return `{ "enable_m365_public_endpoint": true }`.
</details>

<details><summary><b>Step 4 — Fetch the helper's identity and tenant for the Bot</b></summary>

> [!NOTE]
> Read-only lookup, nothing is changed. The hosted agent
> has its own **managed identity** (a `client_id`). You copy that id plus your
> tenant id because the Bot in Step 5 must be created *as* that identity — that's
> the trust link that lets the agent's replies be accepted by Teams.

```powershell
$ProjectEndpoint = "https://<foundry>.services.ai.azure.com/api/projects/<project>"
$agent = az rest --method get `
  --url "$ProjectEndpoint/agents/agent-activity?api-version=v1" `
  --resource https://ai.azure.com | ConvertFrom-Json
$agent.instance_identity                 # client_id + principal_id
az account show --query tenantId -o tsv  # tenant
```

Use the `client_id` as the Bot's `msaAppId` and the tenant as `msaAppTenantId`
in Step 5.
</details>

<details><summary><b>Step 5 — Create the Azure Bot + Teams channel</b> → <a href="bot-service.bicep"><code>bot-service.bicep</code></a></summary>

> [!NOTE]
> The Azure Bot is the **Teams-facing front door**. This
> deploys that Bot resource, points its messaging endpoint at the agent's
> activity URL, and turns on the **Teams channel**. It uses the agent's identity
> from Step 4 as `msaAppId`, so messages flow in and the agent's replies flow
> back out through the same trusted identity.

```powershell
$SubscriptionId = "<your-subscription-id>"
$ResourceGroup  = "<your-resource-group>"
$BotName        = "bot-agent-activity"
$AgentClientId  = "<from Step 4>"
$TenantId       = "<from Step 4>"
$ActivityEndpoint = "https://<foundry>.services.ai.azure.com/api/projects/<project>/agents/agent-activity/endpoint/protocols/activityProtocol?api-version=2025-05-15-preview"

az account set --subscription $SubscriptionId
az provider register --namespace Microsoft.BotService | Out-Null
az deployment group create `
  --resource-group $ResourceGroup `
  --template-file ./bot-service.bicep `
  --parameters `
    botName=$BotName `
    displayName="Activity-Protocol Helper" `
    msaAppId=$AgentClientId `
    tenantId=$TenantId `
    endpoint=$ActivityEndpoint
```

Creates the Bot (single-tenant, msaAppId = the helper's own instance identity)
and the Teams channel, with messagingEndpoint = the helper's activity URL.
</details>

<details><summary><b>Step 6 — Grant the helper managed identity access to storage (for durable state)</b></summary>

> [!NOTE]
> The helper needs to *remember* things between messages —
> who's already been shown the preference card, conversation history, and which
> Foundry session belongs to which Teams chat. It keeps that in Azure Blob
> storage. This grants the agent's identity permission to read/write that
> storage. Skip it and the helper falls back to memory, so it forgets everything
> on every restart.

```powershell
$scope = "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<storage-account>"
az role assignment create `
  --assignee $AgentClientId `
  --role "Storage Blob Data Contributor" `
  --scope $scope
```

`prefs.py` + `state_store.py` persist the preference "asked?" flag,
per-conversation history, and the session-id mapping in an `agent-state`
blob container that the helper creates on demand. Without this role the
helper falls back to in-memory state and every cold start / replica resets
the poll.
</details>

<details><summary><b>Step 7 — Publish to Microsoft 365 / Teams via Foundry publish</b></summary>

> [!NOTE]
> This tells Foundry to package the agent as an installable
> **Teams app**. `publishScope = Shared` means only you can install it; `Tenant`
> means the whole org (needs admin approval). It returns a `teamsAppId`. The
> catch: this path always ships `supportsFiles: false`, so file upload won't
> work from it — that's the entire reason Step 8 exists.

```powershell
$ProjectEndpoint = "https://<foundry>.services.ai.azure.com/api/projects/<project>"
$BotArmId = "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.BotService/botServices/bot-agent-activity"
$body = @{
  agentDisplayName    = "Activity-Protocol Helper"
  botServiceArmId     = $BotArmId
  publishScope        = "Shared"       # Shared = Just you ; Tenant = whole org (admin approval)
  publishAsAutopilot  = $false
  appVersion          = "1.0.0"
  shortDescription    = "Foundry M365 Agent (helper front-door)"
  fullDescription     = "Activity-protocol helper that forwards to an existing Foundry Responses agent."
  developerName       = "<your-org>"
  developerWebsiteUrl = "https://www.example.com"
  privacyUrl          = "https://www.example.com/privacy"
  termsOfUseUrl       = "https://www.example.com/terms"
} | ConvertTo-Json -Depth 10
$tmp = New-TemporaryFile ; Set-Content $tmp -Value $body -NoNewline
az rest --method post `
  --url "$ProjectEndpoint/agents/agent-activity/microsoft365/publish?api-version=v1" `
  --resource https://ai.azure.com `
  --headers "Content-Type=application/json" `
  --body "@$tmp"
Remove-Item $tmp -Force
```

Returns `teamsAppId` + `titleId`. The app now appears in your user's Teams as
a *Custom app*. **But this manifest hard-codes `supportsFiles: false`**, so
Teams still won't hand file uploads to the bot. To enable file upload/return
in Teams, continue to Step 8.
</details>

<details><summary><b>Step 8 — (Optional but needed for files) Build + sideload a custom Teams app with <code>supportsFiles: true</code></b></summary>

> [!NOTE]
> Only needed if you want **file upload/download in Teams**.
> You build your *own* Teams app package (which flips `supportsFiles: true`)
> pointing at the **same** Bot from Step 5, then install it manually (sideload)
> or via a Teams admin. Same bot, same state — the only difference is a manifest
> with file support turned on, which the Step 7 publish can't do.

```powershell
$env:BOT_ID = "<agent-activity instance_identity.client_id from Step 4>"
$env:APP_ID = [guid]::NewGuid().ToString()     # or a stable GUID you keep for updates
python build_package.py
```

Produces `agent-activity-teams.zip` with a manifest whose bot object has
`supportsFiles: true`, `scopes: [personal, team, groupChat]`, and points at
the **same** Bot from Step 5 (so it routes to the same helper, same state).

Install one of:

- **Sideload for yourself:** Teams → **Apps → Manage your apps → Upload an app
  → Upload a custom app** → pick the zip.
- **Submit for org approval:** same menu → the option is called
  *Submit an app to your org* if custom-app upload is disabled for you. An
  admin approves it and it then shows in the org catalog.
- **Distribute to a group:** a Teams admin uploads the same zip via
  Teams Admin Center → **Teams apps → Manage apps → Upload new app**, then
  assigns it via a **Setup policy** targeted at the Entra group of your users.

**When to build the zip:** if you want files at all in Teams. The Foundry
`microsoft365/publish` output from Step 7 doesn't turn file support on and
there's no manifest override in that API. See the *Limitations* section.
</details>

<details><summary><b>Step 9 — Install and test in Teams</b></summary>

> [!NOTE]
> The payoff — actually use it in Teams and confirm each
> feature works end to end (welcome/preference card, file upload, generated-file
> consent, and the `RESET**` reset). App Insights lets you watch the agent and
> its tools actually run behind the scenes.

- Personal chat with the app, send `hello` — expect the welcome message +
  preference card on first contact.
- Attach a `.pptx` / `.pdf` / etc. and reference it in your message —
  expect `Received 1 file(s): …`, then a normal reply from the target agent.
- Ask the agent to *"write a short doc about black holes"* — expect a
  file-consent card. Click **Allow** and the file lands in your OneDrive.
- Type `RESET**` — clears conversation memory + resurfaces the preference
  card.

Check `AppRequests` in the project's App Insights to see each `invoke_agent`
and any `tools/call ...` from the target agent's MCP tools.
</details>

## Limitations / trade-offs

- **File upload from Teams requires the sideloaded manifest.** Foundry's
  `microsoft365/publish` (Step 7) always emits `supportsFiles: false`, and
  the API has no override. So while the **code** in `sessions.py` and
  `outfiles.py` fully implements inbound + outbound files, files only
  actually work when the app is installed from the **custom package** built
  in Step 8. And installing that package requires either self-serve sideload
  permission or a Teams admin — which is exactly the wall you'll hit in
  locked-down tenants.
- **Delivery is same-tenant-reliable only.** The Bot is single-tenant.
  When users in a *different* tenant install the app (custom or Foundry-
  published) and message it, Bot Framework's delivery to Foundry's
  activity-protocol server behind `enable_m365_public_endpoint` is
  intermittent. Same-tenant users get reliable delivery. Cross-tenant is a
  known-flaky path. (Folder 3's translator-container pattern exists
  specifically to solve this: give the bot a *public* endpoint you own.)
- **You still can't reliably detect the native "Remove chat history" click.**
  Teams does not emit a bot activity for that action. Folder 6 uses a
  typed `RESET**` command as the deterministic reset trigger.
- **The Foundry Responses session is per-conversation.** Files and history
  live in that session sandbox; they're gone when the session TTL expires
  server-side. The helper's `state_store` persists the *pointer* (which
  session id belongs to which Teams conversation), not the files themselves.
- **Requires managed environments + Azure roles.** You need Owner (or
  Contributor + UAA) on the subscription, plus `Storage Blob Data
  Contributor` on the state storage account, plus a Foundry User role on
  the project.

## Differences from folders 4 and 5

|  | Folder 4 (REST + APIM) | Folder 5 (REST, no APIM) | Folder 6 (BYO Activity) |
|---|---|---|---|
| Who implements Activity Protocol | Foundry | Foundry | **You** (this folder) |
| Bot messagingEndpoint | APIM public URL | Foundry activity URL | Foundry activity URL (of the *helper* agent) |
| Manifest control | None (server-generated) | None (server-generated) | **Full** (via `build_package.py`) |
| `supportsFiles` in Teams | No | No | **Yes** (in the custom package) |
| Custom loading indicator | No | No | **Yes** (`main.py` typing indicator) |
| Custom welcome / prefs card | No | No | **Yes** (`prefs.py`) |
| Durable per-user state | No | No | **Yes** (Blob-backed) |
| Requires sideload for files | — | — | **Yes** (Foundry publish still ships `supportsFiles: false`) |

Folders 4 and 5 are the *no-code, no-manifest* paths. Folder 6 is the
*code + manifest* path — you own the Teams UX in exchange for the sideload
step and the durable-state infra.

## Learn more

- [foundry-samples — `bring-your-own/activity/echo`](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity/echo)
  — the minimal Activity Protocol skeleton that this folder started from.
- [foundry-samples — `bring-your-own/activity/github-copilot`](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity/github-copilot)
  — the fuller pattern (streaming, adaptive cards, file consent, org-catalog
  install guidance) this folder's UX is modeled on.
- [Handle files and file consent in Microsoft Teams](https://learn.microsoft.com/microsoft-365/agents-sdk/teams/teams-files) — `supportsFiles`, `fileConsent/invoke`, OneDrive upload flow.
- [Publish agent to Copilot via a private network](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network) — the `microsoft365/publish` API used in Step 7.
- [Allow Microsoft 365 traffic to a private-network agent](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-agent?tabs=portal#allow-microsoft-365-traffic-to-a-private-network-agent) — the `enable_m365_public_endpoint` switch used in Step 3.
- [Teams app package + sideload / org catalog](https://learn.microsoft.com/microsoftteams/platform/concepts/deploy-and-publish/apps-upload) — install paths for Step 8.
