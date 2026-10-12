# 6 — BYO Activity Protocol (hosted agent) powered by the GitHub Copilot SDK, linked to your target agent

> Deploy your **own** hosted agent that speaks the Foundry **Activity Protocol**
> natively and runs its own model + tool loop with the
> [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/). It
> **publishes to Teams as one of your existing Foundry prompt agents** (same
> instructions, model and toolbox) and adds the Teams UX the Foundry-managed
> publish path in [folder 5](../5-native-rest-api-activity-protocol-routing/)
> can't: streaming replies, file reads, file downloads, sign-in cards for
> per-user MCP tools, adaptive cards, and on-demand toolbox skills.

## Goal

Same target audience as folders 4–6 (Teams users), but instead of forwarding
each turn to a second agent (as [`6-…-echo`](../6-native-BYO-activity-protocol-echo/)
does), this helper **becomes** your target agent in a single agent/session:

- **Link a target agent.** `link-target-agent.ps1` pulls an existing Foundry
  prompt agent's instructions, model and toolbox into `target-agent.yaml`; the
  helper adopts them at startup.
- **Use any MCP tools from your toolbox.** The model lists the toolbox's tools
  and calls any of them by name. Nothing is hard-coded to a particular tool.
- **Per-user OAuth tools work in Teams.** When a toolbox connection needs the
  user to sign in (OAuth identity passthrough), the helper catches
  `CONSENT_REQUIRED` and shows a **sign-in card**, then forwards the per-request
  call id so the toolbox acts as that user.
- **Toolbox skills on demand.** Skills published on the toolbox (SKILL.md) are
  listed in one line each; the model loads a skill's full text only when a
  request matches it.
- **Files both ways.** Users attach files (and images) in Teams; the model
  creates `.docx` / `.pptx` / `.pdf` / text files with its own Python and hands
  them back through the Teams **File Consent** flow.
- **Durable logs.** Optional App Insights export, including each user message
  and agent reply, that survives session resets and redeploys.

Reference — [`foundry-samples/…/bring-your-own/activity/github-copilot`](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/bring-your-own/activity).

## How it flows

```
Teams user
   |  (Azure Bot messaging endpoint = this hosted agent's activity URL)
   v
activity-agent  (this folder, Foundry hosted agent, agent identity)
   |  GitHub Copilot SDK session (one per Teams conversation)
   |     model: your Foundry deployment (Responses API, agent-identity token)
   |     prompt: target agent's instructions + skill index
   |
   +--> built-in shell / python / file tools   (read uploads, build documents)
   +--> list_toolbox_tools / call_toolbox_tool  -> Foundry toolbox (MCP)
   |        CONSENT_REQUIRED -> sign-in card in Teams
   +--> get_skill                               -> toolbox skills (SKILL.md)
   +--> deliver_file                            -> Teams File Consent -> OneDrive
```

Every Teams turn:
1. Downloads any attachments and hands them to the model.
2. Streams the model's reply into Teams while it runs tools.
3. After the stream closes, sends any queued UI as its own message: a sign-in
   card, the to-do board, or a file-consent card (retried on 429/5xx).
4. On **Allow**, uploads the file bytes to the user's OneDrive and posts the
   download card.

## Prerequisites

- **Tools:** Azure CLI 2.60+, Azure Developer CLI (`azd`) 1.34+ with the
  `azure.ai.agents` extension, PowerShell 7+, Python 3.10+ on `PATH` (for the
  linker). Docker is **not** needed: `azd deploy` builds remotely.
- **An existing Foundry project** with a model deployment the agent can use
  (for example `gpt-5.2`). This folder never creates a Foundry account,
  project or model.
- **Optional:** a Foundry **prompt agent** to link (its instructions + model +
  toolbox) and a **toolbox** holding your MCP tools and skills.
- **Roles:** see [Roles / RBAC](#roles--rbac). Every grant this deployment
  needed is listed there.
- **Teams:** for sideloading, *Upload a custom app* must be allowed for your
  account; for org-wide publishing, a Teams admin approves the app.

## Roles / RBAC

| Who | Role / permission | Scope | Why |
|---|---|---|---|
| You (deployer) | **Contributor** (or Owner) | Resource group | `azd deploy` creates the Azure Bot + Teams channel; you create App Insights. |
| You (deployer) | **Foundry User** (formerly *Azure AI User*) | Foundry project | Create agent versions and sessions, read the target agent for linking, call the publish API. |
| You (deployer) | **User Access Administrator** or **Owner** | Foundry project (or RG) | `deploy.ps1` grants the agent identity *Foundry User* (next row). Skip if an admin does that grant. |
| **Agent identity** (`<agent>-AgentIdentity` service principal) | **Foundry User** | Foundry project | **Read toolbox skills.** Without it the toolbox answers `skills/list` with an empty list and no error (tools still work), so the agent says no skills exist. `deploy.ps1` grants this automatically (`GrantAgentIdentityFoundryUser`). |
| Subscription admin (one time) | Register the `Microsoft.BotService` resource provider | Subscription | `azd deploy` can't create the Azure Bot until the provider is registered. Needs subscription Contributor. |
| Entra admin (only for OAuth MCP connections) | **Cloud Application Administrator** or **Application Administrator** | Tenant | Grant admin consent on the Entra app your toolbox connection uses for OAuth identity passthrough. |
| Each end user (only for OAuth MCP connections) | Sign in once through the **sign-in card** | Their own account | The toolbox runs that user's tool calls under their identity. No Azure role. |
| Teams admin (only for `Tenant` publish) | Approve the app in Teams Admin Center | Tenant | `PublishScope = 'Tenant'` submits the app to the org catalog. `Shared` needs no admin. |

> [!NOTE]
> **Not needed:** subscription-scope `Microsoft.Resources/deployments/write`.
> `deploy.ps1` renders `azure.yaml` with your existing project's `endpoint` and
> `deployments: []`, so azd never runs a subscription-scope deployment to
> create a new project or model.
>
> **Not needed:** any role for the agent to call the model or the toolbox's
> tools. A hosted agent's identity gets that access from the platform.

## What's in this folder

- `deploy.config.example.psd1` — every value you fill in (copy to `deploy.config.psd1`, git-ignored).
- `deploy.ps1` — the one command you run: renders `azure.yaml`, links the target agent, sets App Insights, runs `azd deploy`, deletes sessions still on old versions, grants the agent identity *Foundry User*, and stamps the Teams package.
- `link-target-agent.ps1` + `fetch_target_agent.py` — pull a Foundry prompt agent's instructions, model and toolbox into `src/activity-agent/target-agent.yaml`.
- `publish.ps1` — publish the deployed agent to Microsoft 365 / Teams through the Foundry `microsoft365/publish` API.
- `src/activity-agent/` — the hosted agent:
  - `main.py` — `ActivityAgentServerHost` + `message` / `invoke` handlers; streams replies, then delivers queued cards/files; logs each turn.
  - `client.py` — GitHub Copilot SDK harness: one session per conversation, your Foundry model as provider, system prompt = persona + target instructions + skill index.
  - `tools.py` — custom tools: to-do list, `deliver_file` (rejects corrupt Office files), `list_toolbox_tools` / `call_toolbox_tool`, `get_skill`.
  - `toolbox.py` — consent-aware MCP client for the linked toolbox (tools + skills).
  - `target_agent.py` — loads `target-agent.yaml` (or `TARGET_AGENT_ID`) and builds the system prompt.
  - `files.py` — downloads files/images users share in Teams.
  - `outfiles.py` — Teams File Consent card + `fileConsent/invoke` upload to OneDrive.
  - `cards.py` — to-do Adaptive Card and the toolbox sign-in card.
  - `target-agent.example.yaml`, `.env.example`, `Dockerfile`, `requirements.txt`.

## Step Log

### Step 1 — Install the tools and sign in

<details><summary><b>1a — Install the CLIs and the azd agents extension</b></summary>

```powershell
winget install Microsoft.AzureCLI
winget install Microsoft.Azd
azd extension install azure.ai.agents
```
</details>

<details><summary><b>1b — Sign in to the tenant and subscription that hold your Foundry project</b></summary>

```powershell
az login --tenant <tenant-guid>
az account set --subscription <subscription-guid>
azd auth login --tenant-id <tenant-guid>
```

`deploy.ps1` refuses to run if `az` is signed in to a different tenant or
subscription than the config says.
</details>

<details><summary><b>1c — Check that the Bot Service provider is registered</b></summary>

```powershell
az provider show --namespace Microsoft.BotService --query registrationState -o tsv
# Not "Registered"? Someone with subscription Contributor runs:
az provider register --namespace Microsoft.BotService
```
</details>

### Step 2 — Get the deployer roles

<details><summary><b>2a — Contributor on the resource group</b></summary>

```powershell
az role assignment create --assignee <your-upn-or-object-id> --role Contributor `
  --scope /subscriptions/<sub>/resourceGroups/<rg>
```
</details>

<details><summary><b>2b — Foundry User on the Foundry project</b></summary>

The role was renamed from *Azure AI User*; use its id so the name doesn't matter.

```powershell
az role assignment create --assignee <your-upn-or-object-id> `
  --role 53ca6127-db72-4b80-b1b0-d745d6d5456d `
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>
```
</details>

<details><summary><b>2c — User Access Administrator on the project (so deploy.ps1 can grant the agent identity)</b></summary>

Needed only if you want `deploy.ps1` to grant the agent identity *Foundry User*
itself. Otherwise set `GrantAgentIdentityFoundryUser = $false` and have an admin
run Step 7 instead.

```powershell
az role assignment create --assignee <your-upn-or-object-id> --role "User Access Administrator" `
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>
```
</details>

### Step 3 — Prepare your agent, toolbox and tools in Foundry

<details><summary><b>3a — Confirm the model deployment exists</b></summary>

Note its name (Foundry portal → **Models + endpoints**). It goes in
`ModelDeploymentName`; a linked target agent's own model overrides it.
</details>

<details><summary><b>3b — (Optional) Create a toolbox with your MCP tools and skills</b></summary>

Foundry portal → **Tools** → create a toolbox, then **Add** your MCP servers
(any MCP tool: data, APIs, search…) and any **Skills** (SKILL.md documents,
for example a document-generation skill). With **Tool search** on, the toolbox
exposes a search + call pair; the agent handles both shapes.
</details>

<details><summary><b>3c — (Optional) Create the prompt agent you want to publish</b></summary>

Give it the instructions and model you want in Teams, and attach the toolbox
from 3b. Its name goes in `TargetAgentName`. Without a target, the helper keeps
its built-in assistant persona and has no toolbox.
</details>

<details><summary><b>3d — (Only for OAuth MCP connections) Grant admin consent on the connection's Entra app</b></summary>

If a toolbox connection uses OAuth identity passthrough, a **Cloud Application
Administrator** (or Application Administrator) grants admin consent for the
API permissions on that connection's Entra app registration. Each user still
signs in once through the sign-in card the agent shows them.
</details>

### Step 4 — Fill in deploy.config.psd1

<details><summary><b>4a — Copy the example</b></summary>

```powershell
Copy-Item deploy.config.example.psd1 deploy.config.psd1
```

`deploy.config.psd1` is git-ignored. Every `<placeholder>` must be replaced;
the scripts stop on any that are left.
</details>

<details><summary><b>4b — Azure and Foundry identifiers</b></summary>

`TenantId`, `SubscriptionId`, `SubscriptionName`, `AzureLocation`,
`ResourceGroupName`, `FoundryAccountName`, `FoundryProjectName`,
`FoundryProjectResourceId` (portal → project → **Properties** → Resource ID),
`FoundryProjectEndpoint`, `ModelDeploymentName`.
</details>

<details><summary><b>4c — Agent, bot and Teams names</b></summary>

- `AzdEnvironmentName` — any local name for this azd environment.
- `AgentName` / `AgentDisplayName` — the hosted agent (lowercase name, unique in the project).
- `BotName` — the Azure Bot; must be **globally** unique.
- `TeamsAppShortName` (≤ 30 chars), `TeamsAppFullName`, `TeamsAppVersion`.
- Publish fields (`TeamsDisplayName`, `PublishScope`, descriptions, URLs) — used by `publish.ps1`.
</details>

<details><summary><b>4d — Optional switches</b></summary>

- `TargetAgentName` — the prompt agent from 3c; `''` keeps the built-in persona.
- `AppInsightsName` — an App Insights resource in the RG (Step 5); `''` disables export.
- `GrantAgentIdentityFoundryUser` — `$true` (default) grants the agent identity
  *Foundry User* on every deploy; `$false` if an admin does it (Step 7).
</details>

### Step 5 — (Optional) Create App Insights for durable logs

<details><summary><b>5a — Create a workspace and App Insights</b></summary>

```powershell
az monitor log-analytics workspace create -g <rg> -n <law-name> -l <region> --retention-time 30
$ws = az monitor log-analytics workspace show -g <rg> -n <law-name> --query id -o tsv
az monitor app-insights component create -g <rg> -a <appi-name> -l <region> `
  --kind web --application-type web --workspace $ws
```

Put `<appi-name>` in `AppInsightsName`. `deploy.ps1` reads its connection
string at deploy time and stores it only in the git-ignored azd environment.
</details>

<details><summary><b>5b — Know what gets logged</b></summary>

Each turn logs `USER_MESSAGE` and `AGENT_REPLY` **with the message text**, plus
card/file delivery (`UI_*`), toolbox calls (`TOOLBOX_*`), skill use
(`SKILL_TOOL_CALLED`, `SKILLS_LIST`) and errors. Anyone with read access to
the App Insights resource can read users' messages; set
`LOG_MESSAGE_CONTENT=false` on the agent to log metadata only.

> [!NOTE]
> Foundry overrides `APPLICATIONINSIGHTS_CONNECTION_STRING` inside hosted
> containers with an invalid value. The agent therefore receives the string as
> `AGENT_APPINSIGHTS_CONNECTION_STRING` and copies it back into the standard
> variable before the host starts. Look for `APPINSIGHTS_EXPORT configured=True`
> at startup; the platform's own `appinsights_configured=False` line can be ignored.
</details>

### Step 6 — Deploy with deploy.ps1

<details><summary><b>6a — Run it</b></summary>

```powershell
.\deploy.ps1                 # full deploy
.\deploy.ps1 -RenderOnly     # only render azure.yaml, deploy nothing
.\deploy.ps1 -SkipLink       # keep the current target-agent.yaml
.\deploy.ps1 -KeepSessions   # don't reset Teams chats onto the new version
```

Always deploy with `deploy.ps1`, not a bare `azd deploy`: the steps below only
happen in the script.
</details>

<details><summary><b>6b — What the script does, in order</b></summary>

1. Validates the config.
2. Links the target agent (`link-target-agent.ps1`) → `src/activity-agent/target-agent.yaml`.
3. Renders `azure.yaml` bound to your **existing** project (`endpoint`, `deployments: []`).
4. Checks `az` is on the configured tenant/subscription and the project is
   visible, then sets the azd environment values (and the App Insights
   connection string).
5. Runs `azd deploy`: remote build, new agent version, Azure Bot + Teams channel.
6. **Deletes agent sessions not on the new version.** A session stays on the
   version it was created with, so without this an existing Teams chat keeps
   running the old code. It also clears those chats' saved state (to-dos,
   cached files, conversation history).
7. **Grants the agent identity *Foundry User*** on the project (skipped if it already has it).
8. Writes your Teams names into `src/activity-agent/appPackage.zip`.
</details>

<details><summary><b>6c — Check the output</b></summary>

Look for `Deployed agent version: <n>`, any `Deleting session …` lines, the
Foundry User line, and `Deployment verified`. If the role grant prints a
warning, do Step 7.
</details>

### Step 7 — Make sure the agent identity can read toolbox skills

<details><summary><b>7a — Find the agent identity</b></summary>

```powershell
azd ai agent show --output json   # instance_identity.principal_id
```

In Entra it appears as `<foundry>-<project>-<agent>-AgentIdentity`.
</details>

<details><summary><b>7b — Grant Foundry User on the project (if deploy.ps1 didn't)</b></summary>

```powershell
az role assignment create --assignee-object-id <principal_id> `
  --assignee-principal-type ServicePrincipal `
  --role 53ca6127-db72-4b80-b1b0-d745d6d5456d `
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>/projects/<project>
```

Allow a few minutes to apply. No redeploy is needed; the agent retries the
skill lookup until it finds skills.
</details>

<details><summary><b>7c — Confirm in the logs</b></summary>

Ask the agent to "list your skills", then check for `SKILLS_LIST count=<n>`
with `n > 0` and `SKILL_READ … chars=<n>` (Step 10a). `count=0 error=None`
means the role isn't in effect yet.
</details>

### Step 8 — Put the agent in Teams

<details><summary><b>8a — Option A: publish through Foundry</b></summary>

```powershell
.\publish.ps1
```

`PublishScope = 'Shared'` makes it available to you; `'Tenant'` submits it to
the org catalog for a Teams admin to approve. Then open Teams → **Apps** →
**Built for your org**.
</details>

<details><summary><b>8b — Option B: sideload the generated package</b></summary>

Teams → **Apps** → **Manage your apps** → **Upload an app** →
`src/activity-agent/appPackage.zip`. If *Upload a custom app* is greyed out,
an admin enables it under Teams Admin Center → **Teams apps** → **Setup policies**.
</details>

### Step 9 — Test it in Teams

<details><summary><b>9a — Check the version</b></summary>

Send `/version`. The reply shows `agent_version=<n>` (should match Step 6c)
and the `session_id`.
</details>

<details><summary><b>9b — Use a toolbox tool</b></summary>

Ask for something one of your MCP tools does. If its connection needs sign-in,
a **sign-in card** appears: sign in, then ask again.
</details>

<details><summary><b>9c — Files in and out</b></summary>

- Attach a PDF/DOCX/image and ask about it.
- Ask for a document or deck → click **Allow** on the card → open it. (Office
  files with structural damage are rejected before delivery, and the model
  rebuilds them.)
</details>

<details><summary><b>9d — Skills</b></summary>

Ask "what skills do you have?" or a task a skill covers. The logs should show
`SKILL_TOOL_CALLED` on those turns only.
</details>

### Step 10 — Monitor and troubleshoot

<details><summary><b>10a — Read a session's console log</b></summary>

```powershell
azd ai agent sessions list                       # find the active session id
azd ai agent monitor -s <session-id> --tail 300
```
</details>

<details><summary><b>10b — Query App Insights</b></summary>

```kusto
traces
| where timestamp > ago(1h)
| where message has_any ("USER_MESSAGE", "AGENT_REPLY", "UI_", "TOOLBOX_", "SKILL", "Traceback")
| project timestamp, severityLevel, message
| order by timestamp asc
```
</details>

<details><summary><b>10c — Common symptoms and fixes</b></summary>

| Symptom | Cause | Fix |
|---|---|---|
| Teams chat still runs old behavior after a deploy | The chat's session is pinned to the version it started on | Deploy with `deploy.ps1` (deletes stale sessions), or `azd ai agent sessions delete <id>` |
| "No skills are available" though the toolbox has skills | Agent identity lacks *Foundry User* (`SKILLS_LIST count=0 error=None`) | Step 7 |
| Download or sign-in card never appears | Look for `UI_ATTACHMENT_SEND_FAILED` with its status | 429/5xx are retried; a 400 means Teams rejected the card payload |
| PowerPoint offers to *Repair* a generated deck | Duplicate parts from reusing a template's slides | Fixed in `deliver_file` + prompt; if it recurs, check `DELIVER_FILE_REJECTED` |
| App Insights stays empty | Exporter fails on the platform's connection string | Look for `APPINSIGHTS_EXPORT configured=True`; redeploy with `AppInsightsName` set |
</details>

## Limitations / trade-offs

- **One hosted agent per published agent.** Each Teams agent is its own deploy.
- **Sessions reset on deploy.** Deleting stale sessions clears per-chat state;
  use `-KeepSessions` to keep it (those chats stay on the old version).
- **Card/file flows are Teams personal-scope features.** In Microsoft 365
  Copilot they fall back to text.
- **Cross-tenant delivery** has the same caveat as folders 4/5: same-tenant
  works reliably.
- **You maintain the code** in `src/activity-agent/`.

## Learn more

- [Hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents)
- [Hosted agent quickstart (azd)](https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-hosted-agent?pivots=azd)
- [`azure-ai-agentserver-activity`](https://pypi.org/project/azure-ai-agentserver-activity/) · [GitHub Copilot SDK](https://pypi.org/project/github-copilot-sdk/)
