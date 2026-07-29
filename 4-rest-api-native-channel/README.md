# 4 — REST API publish via Foundry's native channel

Publish a private-network Foundry agent to Teams using Foundry's **native
activity-protocol channel** — no custom translator container. APIM routes the
Bot/Teams traffic straight to Foundry's `activityProtocol` endpoint. This is the
**quickest path** when your agent does **not** need an MCP tool that acts as the
signed-in user.

> Need per-user delegated tool auth (OBO)? Use the
> [custom translator](../3-custom-translator/) instead. See the comparison table
> in the [root README](../README.md).

## What each script does

| Script | Purpose |
|---|---|
| [`copy-agents.ps1`](copy-agents.ps1) | Clone existing agents into `-restapi` copies used for the native-channel test. |
| [`enable-activity.ps1`](enable-activity.ps1) | Enable the **activity protocol** + `BotServiceRbac` / `Entra` auth schemes on each agent endpoint. |
| [`bot-service.bicep`](bot-service.bicep) | Bicep for an **Azure Bot** + **Teams channel** pointing at the APIM route. |
| [`deploy-bots.ps1`](deploy-bots.ps1) | Deploy the Azure Bots (via `bot-service.bicep`) for each agent. |
| [`wire-apim.ps1`](wire-apim.ps1) | Add the APIM **operation + policy** that rewrites `/agents/{id}/messages` to the Foundry activity-protocol endpoint, and merges bot app IDs into the API's `validate-jwt` audiences. |
| [`publish.ps1`](publish.ps1) | Call Foundry's **Microsoft 365 publish** API to surface each agent in Teams. |

## Typical order

```powershell
# fill in the <placeholders> at the top of each script first
./copy-agents.ps1      # 1. clone agents -> -restapi copies
./enable-activity.ps1  # 2. enable activity protocol + auth schemes
./deploy-bots.ps1      # 3. create Azure Bot + Teams channel
./wire-apim.ps1        # 4. add APIM route + audiences
./publish.ps1          # 5. publish to Microsoft 365 / Teams
```

## Prerequisites

- Azure CLI signed in (`az login`), with rights on the resource group + APIM.
- An existing **private Foundry** account/project (see
  [`../1-private-foundry-infra/`](../1-private-foundry-infra/)) fronted by **APIM**.
- Replace every placeholder — `<your-subscription-id>`, `<your-resource-group>`,
  `<your-foundry-account>`, `<your-project>`, `<your-apim-name>`,
  `<your-tenant-id>`, `<bot-app-id-1>` / `<bot-app-id-2>` — with your own values.

The example agent names (`search-agent`, `data-agent`) are illustrative; swap in
your own agent names.
