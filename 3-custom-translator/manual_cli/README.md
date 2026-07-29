# 3c — Manual CLI publish

Publish a single agent to Teams by running the PowerShell driver directly — no
UI, no notebook. Best for **scripted / CI** publishing or when you want full
control over each step.

This wraps the shared [`../publish-agent.ps1`](../publish-agent.ps1), which per
invocation:

1. Creates (or reuses) a **SingleTenant Entra app** for the bot.
2. Generates a fresh client secret and writes it straight to **Key Vault**
   (never printed to the console).
3. Deploys [`../publish-agent.bicep`](../publish-agent.bicep) to create the
   **Azure Bot**, **Teams channel**, and the **APIM operation** for the agent.
4. Renders the Teams **manifest** and zips it into `out/<BotShortName>.zip`,
   ready to upload to Teams.

## Prerequisites

- Azure CLI ≥ 2.60, **Bicep** installed, signed in via `az login`.
- **Owner** (or Contributor + User Access Administrator) on the resource group,
  plus **Key Vault Secrets Officer** on the vault.
- The shared platform already deployed — see
  [`../setup/SETUP_SHARED_PLATFORM.md`](../setup/SETUP_SHARED_PLATFORM.md).

## Run

```powershell
# from the 3-custom-translator folder (so relative paths to bicep/manifest resolve)
./publish-agent.ps1 `
  -ResourceGroup           <your-resource-group> `
  -ApimName                <your-apim-name> `
  -FoundryAccount          <your-foundry-account> `
  -FoundryProject          <your-project> `
  -AgentId                 asst_XXXXXXXXXXXXXXXX `
  -DisplayName             "Sales Copilot" `
  -BotShortName            sales-copilot `
  -BotSecretsKeyVaultName  <your-key-vault>
```

Replace every `<...>` placeholder with your own values and set `-AgentId` to the
Foundry agent you want to publish.

## Result

- `out/<BotShortName>.zip` — the Teams app package to upload.
- A new registry row / APIM operation routing this bot's messages through the
  shared container.

> The Streamlit UI ([`../streamlit_app/`](../streamlit_app/)) and the notebook
> ([`../notebook_publisher/`](../notebook_publisher/)) perform the same publish
> using the shared `publisher.py`; use whichever fits your workflow.
