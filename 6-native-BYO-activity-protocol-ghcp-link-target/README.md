# 6 - BYO Activity Protocol with GitHub Copilot SDK and a linked target definition

> Deploy a Foundry-hosted Teams assistant with your own Activity Protocol UX,
> an existing prompt agent's instructions and model, and live toolbox tools and
> skills. Follow the stages below in order. All terminal examples use **PowerShell**.

## Goal

Like the [echo sample](../6-native-BYO-activity-protocol-echo/), you own the Teams
message, card, upload and download handlers. Unlike echo, this helper does **not**
forward each message to the target Responses agent. It snapshots that agent's
definition and runs its **own GitHub Copilot SDK model/tool loop**.

- Adopt a target's instructions, model deployment and MCP/toolbox references.
- Discover and invoke tools across linked toolboxes, including Tool Search gateways.
- Show real per-connector OAuth consent links and resume blocked requests.
- Load toolbox skills on demand, with periodic skill-index refresh.
- Read raw uploads and generate downloadable documents through host file/Python tools.
- Use `/status` and `/new` to inspect or explicitly reset the SDK conversation.

This template contains no customer tenant, subscription, toolbox or target-agent
snapshot. Copy the example configuration and supply your own values.

## How It Flows

```text
Before deployment:
target prompt agent -> linker -> target-agent.yaml -> packaged hosted helper

At runtime:
Teams -> Azure Bot -> Foundry Activity endpoint -> this helper
  -> GitHub Copilot SDK session -> your Foundry model deployment
  -> consent-aware MCP bridge -> linked toolbox consumer endpoint
       -> configured MCP connections / tools / skills
  -> host file and Python tools -> deliver_file -> Teams File Consent -> OneDrive
```

The original target agent is not invoked. Its model and instructions are a
snapshot; toolbox calls remain live. A toolbox's Code Interpreter, if configured,
is distinct from the host's file/Python tools and need not share the same files.
Custom function/OpenAPI/grounding declarations copied from a target are not
automatically implemented; this bridge executes supported MCP/resource routes.

## Prerequisites

- An existing Foundry account/project with a usable model deployment and hosted-agent support.
- Azure CLI, Azure Developer CLI (`azd`, 1.34+ recommended), the `azure.ai.agents`
  extension, PowerShell 7+, and Python 3.10+ for the linker. Hosted runtime: Python 3.13.
- A target prompt agent and toolbox if you want linked behavior; neither is created here.
- For a private project: VPN/connected development host, working private DNS, and
  access to the project API. Teams itself does not connect through your private endpoint.
- Teams custom-app upload permission, or an administrator to distribute the app.
- Approved outbound access to model/tool endpoints, shared-file downloads and any
  package sources needed for runtime document generation. Container builds use ACR remotely.

This folder does not provision your Foundry account, project, model or private network.

## What's In This Folder

| File | Purpose |
| --- | --- |
| [deploy.config.example.psd1](deploy.config.example.psd1) | Placeholder configuration; copy to ignored `deploy.config.psd1`. |
| [deploy.ps1](deploy.ps1) | Link, render manifest, configure azd, deploy, manage old sessions, grant optional identity RBAC, stamp package names. |
| [link-target-agent.ps1](link-target-agent.ps1), [fetch_target_agent.py](fetch_target_agent.py) | Extract the target definition and inspect available MCP tools/skills without requiring user consent at build time. |
| [publish.ps1](publish.ps1) | Publish the deployed helper, not the source target, through Foundry's M365 publish API. |
| [target-agent.example.yaml](src/activity-agent/target-agent.example.yaml) | Manual YAML alternative to extracting a target agent. |
| [client.py](src/activity-agent/client.py), [target_agent.py](src/activity-agent/target_agent.py) | SDK sessions, target snapshot loading and skill-index refresh. |
| [toolbox.py](src/activity-agent/toolbox.py), [auth_retry.py](src/activity-agent/auth_retry.py) | Generic toolbox discovery/calls, consent probing and independent pending operations. |
| [main.py](src/activity-agent/main.py), [cards.py](src/activity-agent/cards.py) | Activity handlers, streaming where supported, consent/task/session cards. |
| [files.py](src/activity-agent/files.py), [outfiles.py](src/activity-agent/outfiles.py), [tools.py](src/activity-agent/tools.py) | Raw uploads, model tools and Teams file delivery. |
| [test_toolbox_bridge.py](test_toolbox_bridge.py) | Isolated bridge, consent, session and attachment regression tests. |

## Step Log

<details><summary><b>Step 1 - Configure the existing project and sign in</b></summary>

From this folder, install the agents extension if needed and copy the config:

```powershell
azd extension install azure.ai.agents
Copy-Item deploy.config.example.psd1 deploy.config.psd1
```

Fill every required placeholder. Use the actual project ARM resource ID,
project endpoint, tenant, subscription, region and an existing model deployment.
Choose a globally unique `BotName`. Keep `ServiceSourcePath = 'src/activity-agent'`.
`TargetAgentName` selects the prompt agent to snapshot; leave it empty only for
the manual-YAML route in Step 4 or the built-in persona.

`AppInsightsName = ''` disables the optional export. If set, that App Insights
resource must already exist in the configured resource group. Message/reply text
logging defaults to enabled; review your data-retention policy before using it.

Sign in yourself; these commands change your local CLI identity/context:

```powershell
$config = Import-PowerShellDataFile ./deploy.config.psd1
az login --tenant $config.TenantId
az account set --subscription $config.SubscriptionId
azd auth login --tenant-id $config.TenantId
az account show --query '{tenant:tenantId,subscription:id,user:user.name}' -o json
```

Check that the result matches the configuration. az and azd maintain separate
authentication caches. Do not commit the filled config, `.azure/`, generated
snapshots, packages or credentials.
</details>

<details><summary><b>Step 2 - Check RBAC and provider registration before deployment</b></summary>

These are separate identities and permissions, not one blanket role grant:

| Identity | Permission / scope | Used for |
| --- | --- | --- |
| Deployer | Contributor or equivalent on the resource group | Create/manage Bot resources and deployment dependencies. |
| Deployer | Foundry User or appropriate agent-management rights on the project | Read the target; create helper versions; configure/publish the helper. |
| Role-granting administrator | Owner, User Access Administrator or equivalent role-assignment permission at the intended scope | Grant project access to the helper identity and consumers. Contributor alone cannot grant roles. |
| Hosted helper identity | Foundry User on the project, as used by this sample | Read linked toolbox definitions and skills; downstream resources may need additional permissions. |
| End user | Required agent access; Foundry Agent Consumer for OAuth identity passthrough | Invoke an RBAC-protected agent and resolve per-user toolbox context. Downstream tool permissions/consent are additional requirements. |
| Subscription administrator | `Microsoft.BotService/register/action` at subscription scope | Register Bot Service once if not already registered. |
| Teams / identity administrator, when applicable | App distribution policies / required OAuth admin consent | Allow installation and the connection's requested permissions. |

Read-only checks for your signed-in user:

```powershell
$config = Import-PowerShellDataFile ./deploy.config.psd1
$ProjectScope = $config.FoundryProjectResourceId
$RgScope = "/subscriptions/$($config.SubscriptionId)/resourceGroups/$($config.ResourceGroupName)"
$MyId = az ad signed-in-user show --query id -o tsv
az role assignment list --assignee $MyId --scope $RgScope --include-inherited -o table
az role assignment list --assignee $MyId --scope $ProjectScope --include-inherited -o table
az provider show --namespace Microsoft.BotService --query registrationState -o tsv
```

An administrator grants missing roles at the narrowest suitable scope. If Bot
Service is not `Registered`, a subscription administrator runs
`az provider register --namespace Microsoft.BotService` and verifies registration.
Group membership, PIM activation and custom-role actions must also be considered;
an empty direct-assignment listing alone is not proof of no access.

The wrapper attempts the helper-identity project grant after deployment when
`GrantAgentIdentityFoundryUser = $true`. Set it to `$false` if an administrator
will grant it. It does not grant user/consumer roles, configure connector OAuth
consent, or grant access to downstream databases/APIs.

For OAuth passthrough, Foundry currently requires the consumer's Entra tenant to
match the hosting project. Supporting multiple connector challenges in this code
does not bypass platform cross-tenant restrictions. See [tool authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-authentication).
</details>

<details><summary><b>Step 3 - Prepare toolbox MCP connections, tools and skills</b></summary>

In your Foundry project:

1. Create a connection for each MCP server with the required authentication.
   For per-user OAuth, configure the actual OAuth client, scopes and exact callback;
   obtain any required administrator consent. Keep client secrets out of YAML/code.
2. Add the MCP sources to a toolbox and reference their project connection IDs.
3. Add any supported skill content, including its `SKILL.md` and supporting resources.
4. Publish a toolbox version and explicitly promote the version you want to **default**.
5. Attach the toolbox to the prompt agent you will link in Step 4.

Use the toolbox **consumer endpoint**:

```text
https://<account>.services.ai.azure.com/api/projects/<project>/toolboxes/<toolbox-name>/mcp?api-version=v1
```

This template normalizes version-pinned Foundry toolbox references to that
endpoint. It follows **published default**, not the newest unpromoted version.
The toolbox name and its authentication connection alias need not be identical.

With Tool Search enabled, `tools/list` can contain search/call gateway tools and
auto-pinned tools, not the complete inventory. The bridge handles this mixed
shape, searches configured MCP sources, and invokes discovered names/schemas.
With Tool Search disabled, it handles direct lists and pagination. Keyword
search alone is never proof that another tool was removed.

Configured toolbox built-ins are reported separately from this helper's host
tools. An arbitrary copied tool declaration is not a guarantee of implementation.
Configured tool-approval requirements are not bypassed by this sample.
</details>

<details><summary><b>Step 4 - Integrate the target agent through YAML</b></summary>

**Option A - extract an existing prompt agent (recommended).** Set
`TargetAgentName` in the config, then run:

```powershell
.\link-target-agent.ps1
```

The linker creates/reuses an isolated venv under `%LOCALAPPDATA%/ghcp-linktool`,
fetches the target's latest definition and writes:

- `src/activity-agent/target-agent.yaml`: effective model, instructions and tools.
- `src/activity-agent/target-agent.schema.json`: audit of the raw source definition
  and best-effort MCP inventory. Consent-gated inventory is deferred to runtime.

**Option B - maintain YAML directly.** Leave `TargetAgentName = ''`, then:

```powershell
Copy-Item ./src/activity-agent/target-agent.example.yaml ./src/activity-agent/target-agent.yaml
```

Edit the copied file with your instructions, model deployment and toolbox
consumer URL/connection reference. Do not include credentials. `deploy.ps1`
automatically refreshes Option A on each real deployment unless `-SkipLink` is
used; it does not overwrite manual YAML when no target is configured.

Local YAML takes precedence over startup `TARGET_AGENT_ID` lookup. The linker
does not publish the source agent, copy its conversation history, or delegate
turns to it. The model override is the target's deployment name, so verify that
deployment exists and the helper can use it.
</details>

<details><summary><b>Step 5 - Deploy the helper and understand the wrapper</b></summary>

```powershell
.\deploy.ps1 -RenderOnly
```

This validates the filled configuration and source entry point and writes
`azure.yaml` locally. It does **not** link, authenticate, grant roles, deploy or
validate live permissions. Review the rendered existing-project endpoint.

```powershell
.\deploy.ps1
```

The real wrapper runs these actions in order:

1. Refreshes `target-agent.yaml` when `TargetAgentName` is set, unless `-SkipLink`.
2. Renders `azure.yaml` with the configured source, runtime, CPU/memory, Activity
   protocol, `BotServiceRbac`, existing project endpoint and `deployments: []`.
3. Checks az tenant/subscription and project visibility, then selects/creates the
   configured **local azd environment**. The target-link step occurs before these
   checks, so sign into the intended tenant first.
4. Sets azd values for the existing project/model and Bot name. If configured,
   reads the existing App Insights connection string into azd state without
   adding it to source files.
5. Calls `azd deploy --no-prompt`: packages the code, builds remotely, registers a
   helper version and uses the agents extension's Bot/channel/package flow.
6. Reads the deployed version. By default, deletes sessions on earlier versions
   so new messages can use the new code. This destroys those sessions' history,
   tasks, pending retries and cached uploads. Re-upload templates afterward.
7. Attempts the optional helper-identity Foundry User grant, then stamps names
  and version in `src/activity-agent/appPackage.zip` and verifies the names.

`-KeepSessions` skips deletion, but retained chats can keep running old versions.
`-SkipLink` packages the existing snapshot; it does not fetch target changes.
Neither switch makes model/instruction changes live automatically.

The wrapper does not create your source prompt agent or toolbox, register the
provider, enable the private-project M365 exception, or call `publish.ps1`.

If the identity grant was skipped or warned, an administrator can use the
deployed agent's `instance_identity.principal_id` (not its client ID):

```powershell
$config = Import-PowerShellDataFile ./deploy.config.psd1
$Agent = az rest --method get --url "$($config.FoundryProjectEndpoint)/agents/$($config.AgentName)?api-version=v1" --resource https://ai.azure.com | ConvertFrom-Json
az role assignment create --assignee-object-id $Agent.instance_identity.principal_id `
  --assignee-principal-type ServicePrincipal --role 53ca6127-db72-4b80-b1b0-d745d6d5456d `
  --scope $config.FoundryProjectResourceId
```

Verify the role assignment and allow propagation. Empty skills can also mean an
empty default toolbox version, network errors or insufficient access; do not
diagnose every empty list as RBAC alone.
</details>

<details><summary><b>Step 6 - Enable the M365 endpoint for private-network Foundry</b></summary>

**Required when public network access is disabled.** Run management requests
from a machine that can reach the private project API. Keep the account private:
this opens only Foundry's source-IP-filtered Activity route for Microsoft 365 / Bot
Service traffic. It does not expose the project's Responses, MCP or management APIs.

Read the current endpoint configuration first:

```powershell
$config = Import-PowerShellDataFile ./deploy.config.psd1
$AgentUrl = "$($config.FoundryProjectEndpoint)/agents/$($config.AgentName)?api-version=v1"
az rest --method get --url $AgentUrl --resource https://ai.azure.com --query agent_endpoint
```

For this **Activity-only, Shared/RBAC** sample, enable the exception:

```powershell
$body = @{
  agent_endpoint = @{
    protocol_configuration = @{
      activity = @{ enable_m365_public_endpoint = $true }
    }
    authorization_schemes = @(@{ type = 'BotServiceRbac' })
  }
} | ConvertTo-Json -Depth 10
$temporary = New-TemporaryFile
try {
  Set-Content -LiteralPath $temporary -Value $body -Encoding utf8 -NoNewline
  az rest --method patch --url $AgentUrl --resource https://ai.azure.com `
    --headers 'Content-Type=application/merge-patch+json' --body "@$temporary"
} finally {
  Remove-Item -LiteralPath $temporary -Force
}
az rest --method get --url $AgentUrl --resource https://ai.azure.com `
  --query agent_endpoint.protocol_configuration.activity
```

Verify `enable_m365_public_endpoint: true`. Recheck after redeployment.

**Important:** this PATCH replaces `protocol_configuration` and
`authorization_schemes`. If your endpoint has other protocols or authorization
schemes, include them in the body rather than discarding them. `Tenant` publishing
uses `BotServiceTenant`; choose the scheme to match the intended publish scope.
Public routing does not remove authentication, tenant or RBAC requirements.

Source: [Publish with private-network Foundry](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network).
</details>

<details><summary><b>Step 7 - Publish or install the helper in Teams</b></summary>

For the Foundry M365 publishing route, fill the publishing fields in the config:

```powershell
.\publish.ps1
```

This checks az context and the configured Bot, then POSTs the **helper's**
`/agents/<helper>/microsoft365/publish` endpoint. `Shared` uses RBAC-restricted
visibility; `Tenant` uses tenant-wide visibility/authorization and can require
Teams administrator approval. Publishing can replace the Bot Service auth scheme.

Alternatively, upload `src/activity-agent/appPackage.zip` through Teams **Apps >
Manage your apps > Upload an app**. The package must target the deployed Bot and,
for file workflows, its bot entry must have `supportsFiles: true` and personal
scope. Inspect the generated manifest; catalog publishing is not a substitute
for validating file support. Follow your tenant's app approval policy.

This sample uses the azd agents extension's Bot/channel flow. Do not separately
deploy echo's Bot or storage template: echo has a different runtime and state design.
</details>

<details><summary><b>Step 8 - Test tools, consent, skills, files and session commands</b></summary>

1. Send `/version` and compare the reported version with the deployment output.
2. Ask what tools and skills are available. Confirm each configured MCP source,
   not only matches from a broad keyword search.
3. Request an actual authorized tool operation. If a consent card appears,
   complete sign-in, then click **I've signed in - retry** or send `done`.
4. Complete any additional connector challenges that actually appear. There is
   no assumed tenant, account or sign-in count. Foundry and the downstream API
   still enforce their own tenant and access policies.
5. Request a task covered by a skill and confirm the skill is loaded.
6. Upload a template or document, then request a real generated download. Use
   **Allow** on the Teams File Consent card and open the resulting file.

| Command | What it does | What it does not do |
| --- | --- | --- |
| `/status` | Shows SDK session age, idle time, turn count and stale-reminder threshold; offers **Start Fresh**. | Does not reset the conversation. |
| `/new` or **Start Fresh** | Rotates the persisted SDK nonce/session ID, drops the cached SDK session, resets counters and clears pending auth operations. | Does not create a new Foundry-hosted session, revoke connector consent, delete uploaded files or clear tasks. |
| `/version` | Shows platform version/session identifiers and release label. | Does not switch a chat to another deployed version. |

`SESSION_TIMEOUT_HOURS` defaults to 4. Idle expiry is **a reminder only**, not an
automatic reset. SDK metadata persists under the hosted session's `$HOME`.
Deleting that hosted session during deployment is broader than `/new` and also
removes its saved files/state.

Consent retries are user/conversation/SDK-session bound and valid for 20 minutes
per pending entry. Separate operations are preserved independently; a card claims
its own operation and a normal message selects the oldest pending one. A consumed
retry ID cannot run again. Browser sign-in sends no completion callback; there is
no background polling. Credentials remain with the connection provider; local
retry state stores request/operation data, not connection tokens or consent URLs.
</details>

<details><summary><b>Step 9 - Change toolboxes without confusing runtime updates with redeployment</b></summary>

| Change | Redeploy helper? | Action |
| --- | --- | --- |
| Tools/MCP sources/skills within the same linked Foundry toolbox | No | Publish a version and promote it to **default**. Runtime discovery uses the consumer endpoint. Skill index refresh is cached for about 60 seconds and occurs on subsequent requests. |
| New unpromoted toolbox version | No change reaches consumers | Test it, then promote the intended version to default. Newest-created is not automatically selected. |
| Connection credential repair, downstream grants or renewed user consent | Usually no code redeploy | Repair the actual connection/backend, verify callback/scopes, and test again. Do not recreate the toolbox by default. |
| Target instructions/model or the linked toolbox name/project/endpoint | Yes | Relink or edit YAML, inspect the snapshot, then deploy. Target-definition edits alone do not update a packaged helper. |
| Helper Python, dependencies, session settings, CPU/memory or other packaged environment | Yes | Change source/configuration and redeploy; deal deliberately with old sessions. |
| Teams names, manifest capabilities or distribution scope | Package/publish update | Update configuration, rebuild/stamp or republish as needed, and install the updated app. |

This behavior is specific to Foundry toolbox consumer endpoints. Arbitrary
external MCP URLs retain their own versioning and refresh behavior. Revoked or
unavailable tools still return errors; publishing a default does not guarantee
that downstream auth/networking is healthy.
</details>

<details><summary><b>Step 10 - Diagnose problems and run local regression tests</b></summary>

Select the configured azd environment before inspecting sessions:

```powershell
$config = Import-PowerShellDataFile ./deploy.config.psd1
azd env select $config.AzdEnvironmentName
azd ai agent sessions list
azd ai agent monitor --session-id <session-id> --type console --tail 200
```

Activity agents are push-based: use Teams or the Bot's Web Chat for runtime
checks, not a Responses-style `azd ai agent invoke`. Web Chat does not validate
Teams personal-scope file-consent behavior.

| Symptom | Check first |
| --- | --- |
| Private-project agent is silent | Step 6 flag, Bot endpoint, authorization, consumer access and console errors. |
| Old behavior after redeploy | `/version`; retained hosted sessions may remain on old code. `/new` resets SDK history only. |
| Missing tools/skills | Actual linked toolbox name/default version, source-specific discovery, identity RBAC and per-source errors. |
| Repeated consent or 401 | Current request identity, actual returned consent URL, connector scopes/callback, credential expiry and downstream permissions. A 401 without a real consent link remains an error. |
| Retry card still shows an old acknowledgment | Use a newly emitted card or send a normal message; verify the new deployed version. |
| File upload/download fails | Personal scope, `supportsFiles`, file-consent response and `UI_ATTACHMENT_SEND_FAILED` logs. |

Run the isolated tests in a dedicated Python environment with `httpx`, `PyYAML`
and `azure-identity` installed. Do not use global/base package installation:

```powershell
$PreviousHome = $env:HOME
$TestHome = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
try {
  $env:HOME = $TestHome
  python -B -m unittest discover -s . -p test_toolbox_bridge.py
  if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed.' }
} finally {
  $env:HOME = $PreviousHome
  if (Test-Path $TestHome) { Remove-Item -LiteralPath $TestHome -Recurse -Force }
}
```

These tests mock network/model calls; passing is not live deployment proof.
They cover generic discovery, default routing, consent sequences, independent
claims, claim I/O recovery, successful text containing 401 diagnostics, session
reset/persistence and raw attachment handoff. If claim restoration fails, the
claim is retained/logged for manual recovery, not blindly re-executed.
</details>

## Limitations And Security

- The helper owns execution; linking does not clone every Foundry tool capability.
- Per-user OAuth passthrough must meet Foundry's consumer/tenant requirements and
  each connector's policy. More generic retry handling is not cross-tenant OBO support.
- File Consent delivery and interactive cards depend on the channel/client;
  validate Teams personal scope rather than assuming M365 Copilot parity.
- Runtime shell/Python tools and document-library installation execute code.
  Review tool permission policy, network egress and content logging for production.
- Project-scoped identity permissions and definition inspection can be broader
  than a minimal consumer-only deployment. Review least privilege for your setup.
- Old dependency pins protect Activity SDK compatibility. Upgrade the host and
  M365 Agents packages together and rerun tests before deploying.

## References

- [Echo sample](../6-native-BYO-activity-protocol-echo/) - compare the forwarding architecture.
- [Hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents).
- [Toolbox consumer endpoints and hosted agents](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/use-toolbox-hosted-agent).
- [Toolbox authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-authentication).
- [Private-network M365 publishing](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network).
- [GitHub Copilot SDK](https://github.com/github/copilot-sdk).