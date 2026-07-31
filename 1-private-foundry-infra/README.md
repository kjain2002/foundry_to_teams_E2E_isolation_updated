# 1 — Private Foundry infrastructure

> Step-by-step guide to standing up a **network-isolated Foundry**.
> Format: one-line summary per step → expand for details. Broad → narrow.

## Goal
Provision a Foundry **account + project** with **public network access disabled**
(VNet + private endpoints), so agents run privately. Two IaC options — **Bicep**
(full stack from scratch) or **Terraform** (add a governed project to an existing
account). Pick one.

## Pick your path
**Bicep** = build the whole private stack. **Terraform** = you already have a
Foundry account and want a project + governance.
<details><summary>details</summary>

- `bicep/` — VNet, subnets, private endpoints + DNS, storage, Cosmos, AI Search, the Foundry account + project, role assignments, and the capability host.
- `terraform/` — adds a project + App Insights (tracing/token usage) + optional model deployment + Entra-group RBAC to an **existing** account.
- Deep-dive on the Bicep stack: [`INFRA_PRIVATE_FOUNDRY.md`](INFRA_PRIVATE_FOUNDRY.md).
</details>

---

## Step Log — Bicep path

**Step 1 — Sign in and select your subscription.**
<details><summary>details</summary>

- `az login`
- `az account set --subscription "<your-subscription-id>"`
</details>

**Step 2 — Fill in your parameters.**
<details><summary>details</summary>

- Edit `bicep/main.bicepparam` (and `bicep/add-project.bicepparam`) with your own names, region, and address space.
- Everything is a placeholder until you set it — nothing is hard-coded.
</details>

**Step 3 — Create the resource group and deploy the stack.**
<details><summary>details</summary>

```powershell
cd bicep
az group create -n <your-resource-group> -l <your-region>
az deployment group create -g <your-resource-group> `
  --template-file main.bicep --parameters main.bicepparam
```
- This creates the VNet, private endpoints, data resources, and the Foundry account + project.
</details>

**Step 4 — Create the capability host.**
<details><summary>details</summary>

- Run [`bicep/createCapHost.sh`](bicep/createCapHost.sh) (bash/WSL) to attach the agent capability host to the account/project.
- Tear down later with [`bicep/deleteCapHost.sh`](bicep/deleteCapHost.sh).
</details>

**Step 5 — (Optional) Add more projects.**
<details><summary>details</summary>

- Deploy [`bicep/add-project.bicep`](bicep/add-project.bicep) with `add-project.bicepparam` to add another project to the account.
- Inspect existing resources first with [`bicep/get-existing-resources.ps1`](bicep/get-existing-resources.ps1).
</details>

---

## Step Log — Terraform path

**Step 1 — Configure your inputs.**
<details><summary>details</summary>

```powershell
cd terraform
Copy-Item terraform.tfvars.example terraform.tfvars   # then edit
```
- Set `existing_account_name`, `existing_resource_group`, `project_name`, RBAC groups, and model.
- Dev/prod presets: `environments/dev.tfvars.example`, `environments/prod.tfvars.example`.
</details>

**Step 2 — Authenticate.**
<details><summary>details</summary>

```powershell
az login
$env:ARM_SUBSCRIPTION_ID = "<your-subscription-id>"   # azurerm v4 needs this
```
</details>

**Step 3 — Plan and apply.**
<details><summary>details</summary>

```powershell
terraform init
terraform plan  -out tf.plan
terraform apply tf.plan
```
- Full input reference and outputs: [`terraform/README.md`](terraform/README.md).
- Switch models: [`terraform/MODEL-CHANGE.md`](terraform/MODEL-CHANGE.md).
</details>

---

## Next
Have a private Foundry? Publish an agent to Teams →
[custom translator](../3-custom-translator/) or
[REST API native channel](../4-rest-api-native-channel/).

## File map
<details><summary>Full folder & file hierarchy (click to expand)</summary>

<details><summary><code>&lt;root&gt;</code></summary>

- `README.md` — this file.
- `INFRA_PRIVATE_FOUNDRY.md` — deep-dive on the Bicep private-network stack.
</details>

<details><summary><code>bicep/</code> — full private stack from scratch</summary>

- `main.bicep` / `main.bicepparam` — top-level deployment (VNet, PEs, data resources, Foundry account + project, role assignments).
- `azuredeploy.json` / `azuredeploy.parameters.json` — compiled ARM output of the Bicep stack.
- `add-project.bicep` / `add-project.bicepparam` — add another project to an existing account.
- `createCapHost.sh` / `deleteCapHost.sh` — attach / tear down the agent capability host (bash/WSL).
- `get-existing-resources.ps1` — inspect resources already in the RG before deploying.
- `metadata.json` — template metadata.
- <details><summary><code>modules-network-secured/</code> — reusable Bicep modules</summary>

  - `vnet.bicep` / `existing-vnet.bicep` / `network-agent-vnet.bicep` / `subnet.bicep` — VNet & subnet provisioning.
  - `private-endpoint-and-dns.bicep` — private endpoints + private DNS zones.
  - `standard-dependent-resources.bicep` — storage, Cosmos, AI Search dependencies.
  - `ai-account-identity.bicep` / `ai-project-identity.bicep` / `ai-project-identity-unique.bicep` — Foundry account & project identities.
  - `add-account-capability-host.bicep` / `add-project-capability-host.bicep` — capability host resources.
  - `ai-search-role-assignments.bicep`, `azure-storage-account-role-assignment.bicep`, `blob-storage-container-role-assignments.bicep` / `-unique.bicep`, `cosmos-container-role-assignments.bicep`, `cosmosdb-account-role-assignment.bicep` — RBAC role assignments.
  - `format-project-workspace-id.bicep` — derives the project workspace id.
  - `validate-existing-resources.bicep` — pre-flight validation of existing resources.
  </details>
</details>

<details><summary><code>terraform/</code> — add a governed project to an existing account</summary>

- `main.tf` / `variables.tf` / `outputs.tf` / `providers.tf` / `versions.tf` — the Terraform config.
- `terraform.tfvars.example` / `test.tfvars.example` — input templates.
- `.terraform.lock.hcl` / `.gitignore` — lock file & ignores.
- `README.md` — input reference & outputs. `MODEL-CHANGE.md` — how to switch models.
- <details><summary><code>environments/</code></summary>

  - `dev.tfvars.example` / `prod.tfvars.example` — dev / prod presets.
  </details>
- <details><summary><code>test/</code></summary>

  - `Test-And-Destroy.ps1` — spin up, verify, and tear down for testing.
  </details>
</details>

</details>

*State files, `.terraform/`, and real `*.tfvars` are git-ignored — replace every
`<your-...>` placeholder with your own values.*
