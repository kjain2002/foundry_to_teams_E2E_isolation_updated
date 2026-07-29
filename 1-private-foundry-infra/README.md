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

*State files, `.terraform/`, and real `*.tfvars` are git-ignored — replace every
`<your-...>` placeholder with your own values.*
