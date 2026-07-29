# Foundry → Teams Publisher — Streamlit UI

Browser UI that does the same thing as [../publish-agent.ps1](../publish-agent.ps1)
but with a step-by-step picker:

```
sign in → subscription → Foundry account → project → agent →
display name / branding → publish → download Teams .zip
```

Designed to be deployed **once per tenant** so any team member with
`Azure AI Developer` on a Foundry project can publish their own agent without
ever touching Bicep or PowerShell.

> **Demo:** [pf_<your-python-env>.mp4](https://onedrive.cloud.microsoft/:v:/a@ub3eg2k3/S/cQrZnHlnmBskTJ-AoDHyKHKnEgUCA-ywu7iLUlywEFR5yFPyCA)

---

## When to deploy this vs. stick with the CLI

| Audience | Recommendation |
|---|---|
| Just you / small dev team | Skip this folder. Use [`../publish-agent.ps1`](../publish-agent.ps1). |
| Multiple non-developer users in your org | Deploy this container once. End users self-serve from the browser. |
| Both | Deploy for end users; keep the CLI for ops, CI/CD, batch publishes, break-glass. |

A bot published via the UI can be redeployed or deleted from the CLI and vice
versa — both paths call the **same compiled ARM template**
([`arm/publish-agent.json`](./arm/publish-agent.json), built from
[`../publish-agent.bicep`](../publish-agent.bicep)) and write the bot secret
to the same Key Vault.

---

## Auth pattern

| Mode | When | How |
|---|---|---|
| `sso` (prod) | Container Apps / App Service Easy Auth gates the app | App reads `X-MS-CLIENT-PRINCIPAL` + `X-MS-TOKEN-AAD-ACCESS-TOKEN` from request headers, then uses **MSAL `ConfidentialClientApplication.acquire_token_on_behalf_of`** to exchange for downstream tokens (ARM, Graph, Foundry, Key Vault). |
| `local` (dev) | `streamlit run app.py` on your laptop | `DefaultAzureCredential` (your `az login` session). |

Every downstream call is made **as the signed-in user** — no shared service
principal acting on their behalf. RBAC is exactly the user's RBAC.

### Required tenant access

Delegated permissions on the Streamlit app's own Entra app registration:

| API | Permission | Purpose |
|---|---|---|
| Microsoft Graph | `Application.ReadWrite.OwnedBy` | Create the bot's Entra app + secret |
| Azure Service Management | `user_impersonation` | Deploy `publish-agent.bicep`, rewrite APIM policy |
| Azure AI Services | `user_impersonation` | List Foundry agents |
| Azure Key Vault | `user_impersonation` | Write the bot's client secret to KV |

Plus per-user roles (granted to each end user who will publish):

- `Azure AI Developer` on the Foundry project
- `Contributor` on the publish-target resource group
- `Key Vault Secrets Officer` on `BOT_SECRETS_KEYVAULT_NAME` (RBAC mode)
- `AcrPush` on `TRANSLATOR_ACR_NAME` (build per-bot image)

---

## File layout

```
streamlit_app/
├── README.md              (this file)
├── Dockerfile             multi-stage: bicep build → python:3.12-slim runtime
├── requirements.txt
├── .env.example
├── rebuild-arm.ps1        dev helper — recompiles ../publish-agent.bicep → arm/*.json
├── app.py                 Streamlit UI + step machine
├── auth.py                Easy Auth header parsing + OBO + local DefaultAzureCredential
├── config.py              pydantic env loader
├── discovery.py           ARM REST: list subs / Foundry accounts / projects / agents
├── publisher.py           Graph + ARM + KV + ACR build + APIM policy rewrite + Teams .zip
└── arm/
    └── publish-agent.json compiled from ../publish-agent.bicep at build time
```

---

## Local dev

```powershell
# 1. Compile the ARM template the app deploys
./rebuild-arm.ps1

# 2. Configure env
Copy-Item .env.example .env
# edit .env: AUTH_MODE=local, fill SUBSCRIPTION_ID / RESOURCE_GROUP / APIM_NAME / TRANSLATOR_*

# 3. Sign in
az login --tenant <your-tenant-id>

# 4. Install + run
pip install -r requirements.txt
streamlit run app.py
```

Open <http://localhost:8501>.

---

## Deploy to Azure

Pick one target. Both end up at the same URL pattern with Easy Auth in front.

### Prerequisites for either target

1. The **Streamlit app's Entra app registration** (the SSO front door):

   ```powershell
   $appName = "foundry-teams-publisher-ui"
   $callback = "https://<your-app>.<region>.azurecontainerapps.io/.auth/login/aad/callback"
   $app = az ad app create --display-name $appName `
     --sign-in-audience AzureADMyOrg `
     --web-redirect-uris $callback `
     --enable-id-token-issuance true `
     --enable-access-token-issuance true | ConvertFrom-Json
   az ad sp create --id $app.appId | Out-Null

   # Add API permissions — admin consent required on Application.ReadWrite.OwnedBy
   az ad app permission add --id $app.appId `
     --api 00000003-0000-0000-c000-000000000000 `
     --api-permissions 824c81eb-e3f8-4ee6-8f6d-de7f50d565b7=Scope   # Graph App.RW.OwnedBy
   az ad app permission add --id $app.appId `
     --api 797f4846-ba00-4fd7-ba43-dac1f8f63013 `
     --api-permissions 41094075-9dad-400e-a0bd-54e686782033=Scope   # ARM user_impersonation

   # ...and so on for Azure AI Services + Key Vault. Then admin-consent:
   az ad app permission admin-consent --id $app.appId
   ```

2. A **client secret** for the Entra app, stored in Key Vault:

   ```powershell
   $sec = az ad app credential reset --id $app.appId --years 2 | ConvertFrom-Json
   az keyvault secret set --vault-name <your-kv> `
     --name "streamlit-publisher-client-secret" --value $sec.password
   ```

3. The translator runtime + APIM must already be deployed (`bootstrap-translator.bicep`
   and `bootstrap.bicep` in the parent folder). Have these output values
   handy: `caeName`, `caeDefaultDomain`, `acrName`, `acrLoginServer`, `miName`,
   `miClientId`, `threadTableUrl`, `apimName`, `keyVaultName`.

### Target A — Azure Container Apps (recommended)

Reuses the same CAE as the translator containers — no extra environment to manage.

```powershell
$rg            = "rg-foundry-private"
$cae           = "cae-translator-<suffix>"        # from bootstrap-translator output
$acr           = "acrtranslator<suffix>"          # from bootstrap-translator output
$kv            = "kv-foundry-bots"
$tenantId      = (az account show --query tenantId -o tsv)
$entraClientId = $app.appId
$entraSecretRef= "https://$kv.vault.azure.net/secrets/streamlit-publisher-client-secret"

# 1. Build + push image
az acr build -r $acr -t foundry-teams-publisher:latest .

# 2. Deploy
az containerapp create -g $rg -n ca-foundry-teams-publisher `
  --environment $cae `
  --image "$acr.azurecr.io/foundry-teams-publisher:latest" `
  --target-port 8000 --ingress external `
  --user-assigned (az identity show -g $rg -n mi-translator-<suffix> --query id -o tsv) `
  --registry-server "$acr.azurecr.io" --registry-identity (az identity show -g $rg -n mi-translator-<suffix> --query id -o tsv) `
  --secrets entra-client-secret=keyvaultref:$entraSecretRef,identityref:(az identity show -g $rg -n mi-translator-<suffix> --query id -o tsv) `
  --env-vars `
    AUTH_MODE=sso `
    ENTRA_TENANT_ID=$tenantId `
    ENTRA_CLIENT_ID=$entraClientId `
    ENTRA_CLIENT_SECRET=secretref:entra-client-secret `
    SUBSCRIPTION_ID=(az account show --query id -o tsv) `
    RESOURCE_GROUP=$rg `
    APIM_NAME=apim-foundry-<suffix> `
    BOT_SECRETS_KEYVAULT_NAME=$kv `
    TRANSLATOR_CAE_NAME=$cae `
    "TRANSLATOR_CAE_DEFAULT_DOMAIN=<from bootstrap output>" `
    TRANSLATOR_ACR_NAME=$acr `
    "TRANSLATOR_ACR_LOGIN_SERVER=$acr.azurecr.io" `
    TRANSLATOR_MI_NAME=mi-translator-<suffix> `
    "TRANSLATOR_MI_CLIENT_ID=<from bootstrap output>" `
    "TRANSLATOR_THREAD_TABLE_URL=<from bootstrap output>"

# 3. Enable Easy Auth (AAD)
$appUrl = "https://$(az containerapp show -g $rg -n ca-foundry-teams-publisher --query properties.configuration.ingress.fqdn -o tsv)"
az containerapp auth update -g $rg -n ca-foundry-teams-publisher `
  --enabled true --action RedirectToLoginPage --redirect-provider AzureActiveDirectory
az containerapp auth microsoft update -g $rg -n ca-foundry-teams-publisher `
  --client-id $entraClientId `
  --client-secret-name entra-client-secret `
  --tenant-id $tenantId
```

The app is now at `$appUrl`. First visit prompts SSO; after sign-in the user
lands on the Streamlit picker.

### Target B — Azure App Service (Linux container)

For tenants standardised on App Service. Same image, same env vars; Easy Auth
is configured via App Service's **Authentication** blade instead of
`containerapp auth`.

```powershell
$rg     = "rg-foundry-private"
$plan   = "asp-publisher-linux"
$site   = "app-foundry-teams-publisher"
$acr    = "acrtranslator<suffix>"

# 1. App Service Plan (P1v3 needed for VNet integration if you want to keep it private)
az appservice plan create -g $rg -n $plan --sku P1v3 --is-linux

# 2. Web app from the container image (acr admin user must be enabled, or use MI pull)
az webapp create -g $rg -p $plan -n $site `
  --deployment-container-image-name "$acr.azurecr.io/foundry-teams-publisher:latest"
az webapp config container set -g $rg -n $site `
  --container-registry-url "https://$acr.azurecr.io" --enable-app-service-storage false

# 3. App settings — same set as Container Apps target (use az webapp config appsettings set)
# 4. Authentication — Portal → Authentication → Add identity provider → Microsoft (tenant-only)
# 5. (Optional) Networking → VNet integration → same Foundry VNet, route all traffic
```

---

## Bot secrets policy

Every publish generates a **fresh client secret** on the bot's Entra app and
writes it **directly to Key Vault** as `bot-<short>-secret`, tagged with
`botAppId`, `botShortName`, `createdBy`, `createdAt`. The raw value never
appears in browser memory after the success screen renders.

Why persist it at all when Foundry's hosted endpoint doesn't consume it?

1. **No orphan credentials.** Every secret in the AAD app's credential list
   has a known location — what enterprise security teams audit for.
2. **Future-proofing.** If anyone later adds a custom adapter (proactive
   messages, pre/post hooks), the secret is already where it needs to be.

---

## What gets deployed per publish

| Resource | Lifetime | Cost |
|---|---|---|
| Entra app `bot-<short>-app` + SP + fresh secret | Permanent | $0 |
| `bot-<short>-secret` in Key Vault | Permanent (rotated each publish) | ~$0 |
| `Microsoft.BotService/botServices/bot-<short>` (F0) | Permanent | $0 |
| `MsTeamsChannel` on that bot | Permanent | $0 |
| Translator image `translator:<timestamp>` in shared ACR | Permanent (cleanup via ACR retention policy) | ~$0 |
| `Microsoft.App/containerApps/ca-bot-<short>` in shared CAE | Permanent | Scale-to-zero — ~$0 idle |
| APIM operation `POST /bot/agents/{agentId}/messages` | Permanent | Shared APIM cost |
| `allowed-bot-audiences` named value updated + api-base policy rewritten | n/a | $0 |
| Teams `.zip` downloaded by user | n/a | $0 |

Only the shared APIM + CAE have ongoing cost; per-bot resources scale to zero.

---

## See also

- [../README.md](../README.md) — full publish-agent walkthrough + architecture
- [../../README.md](../../README.md) — top-level repo README with demo video link
- [`publisher.py::update_api_base_policy`](./publisher.py) — the JWT policy rewrite logic
