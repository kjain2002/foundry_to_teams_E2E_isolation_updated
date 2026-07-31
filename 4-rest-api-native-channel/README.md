# 4 — REST API publish via Foundry's native channel

> Step-by-step guide to publishing an agent to Teams using Foundry's **native
> channel** (no custom container). Format: one-line summary per step → expand for
> details. Broad → narrow.

## Goal
Get a private-network Foundry agent into Teams with **plain REST calls + APIM
routing**, using Foundry's built-in `activityProtocol` endpoint. Fastest path
**when your agent doesn't need an MCP tool that acts as the signed-in user**.
(Need per-user tool auth? Use the [custom translator](../3-custom-translator/).)

## How it flows
Teams → Azure Bot → APIM → Foundry's activity-protocol endpoint. Foundry does the
Bot⇄agent translation itself.
<details><summary>details</summary>

- No compute you own — Foundry hosts everything; APIM just routes.
- Each agent gets an Azure Bot + Teams channel; APIM rewrites `/agents/{id}/messages` to the activity-protocol endpoint.
- Compare with the container approach in the [root README](../README.md).
</details>

---

## Step Log

**Step 0 — Create one Entra app per bot (gives you each `BotAppId`).**
<details><summary>details</summary>

```powershell
az ad app create --display-name "bot-<your-agent>-restapi" --sign-in-audience AzureADMyOrg --query appId -o tsv
```
- Do this once per agent; paste the `appId` into `$Agents[].BotAppId` in the next step.
</details>

**Step 1 — Configure once: copy the example config and fill it in.**
<details><summary>details</summary>

```powershell
Copy-Item config.example.ps1 config.ps1   # config.ps1 is git-ignored
```
- Set your Foundry account/project, subscription, RG, tenant, APIM.
- Fill the `$Agents` list — one row per agent (`Agent`, `RestName`, `BotName`, `BotAppId`, `Display`, `Short`, `Full`).
- **Every script below loops over `$Agents`, so this is the only file you edit.**
</details>

**Step 2 — Clone each agent into a `-restapi` copy.** → [`copy-agents.ps1`](copy-agents.ps1)
<details><summary>details</summary>

- `./copy-agents.ps1`
- Reads each `$Agents[].Agent` and creates the `RestName` copy used for the native-channel test.
- Skip if you want to publish your existing agent directly (set `RestName = Agent`).
</details>

**Step 3 — Enable the activity protocol + auth schemes.** → [`enable-activity.ps1`](enable-activity.ps1)
<details><summary>details</summary>

- `./enable-activity.ps1`
- PATCHes each agent endpoint to enable `activity` protocol and the `Entra` + `BotServiceRbac` auth schemes.
</details>

**Step 4 — Create the Azure Bot + Teams channel per agent.** → [`deploy-bots.ps1`](deploy-bots.ps1)
<details><summary>details</summary>

- `./deploy-bots.ps1`
- Deploys [`bot-service.bicep`](bot-service.bicep) for each agent, pointing the messaging endpoint at your APIM route.
</details>

**Step 5 — Wire the APIM route + audiences.** → [`wire-apim.ps1`](wire-apim.ps1)
<details><summary>details</summary>

- `./wire-apim.ps1`
- Adds an APIM operation that rewrites `/agents/{id}/messages` to the Foundry activity-protocol endpoint.
- Merges every `BotAppId` into the API-level `validate-jwt` audience list.
</details>

**Step 6 — Publish to Microsoft 365 / Teams.** → [`publish.ps1`](publish.ps1)
<details><summary>details</summary>

- `./publish.ps1`
- Calls Foundry's `microsoft365/publish` API for each agent, using your developer metadata from `config.ps1`.
</details>

**Step 7 — Install in Teams and test.**
<details><summary>details</summary>

- Open the agent in Teams (via the published app), send a message, confirm it replies.
- Bot messages now travel Teams → Bot → APIM → private Foundry.
</details>

---

## File map
<details><summary>Full folder & file hierarchy (click to expand)</summary>

<details><summary><code>&lt;root&gt;</code> — flat folder (no subfolders)</summary>

- `README.md` — this file.
- `config.example.ps1` — the single config you copy to `config.ps1` and fill in (Foundry account/project, sub, RG, tenant, APIM, and the `$Agents` list). Every script loops over `$Agents`.
- `copy-agents.ps1` — **Step 2:** clone each agent into a `-restapi` copy.
- `enable-activity.ps1` — **Step 3:** PATCH each agent to enable the `activity` protocol + `Entra` / `BotServiceRbac` auth schemes.
- `deploy-bots.ps1` — **Step 4:** deploy `bot-service.bicep` per agent, pointing the messaging endpoint at your APIM route.
- `bot-service.bicep` — the Azure Bot + Teams channel resource deployed per agent.
- `wire-apim.ps1` — **Step 5:** add the APIM operation that rewrites `/agents/{id}/messages` → Foundry's activity-protocol endpoint, and merge each `BotAppId` into the API-level `validate-jwt` audiences.
- `publish.ps1` — **Step 6:** call Foundry's `microsoft365/publish` API per agent.
</details>

</details>

*Fill in `config.ps1` (from `config.example.ps1`) — nothing else needs editing.
`config.ps1`, `_tmp/`, and the runtime policy snapshot are git-ignored.*
