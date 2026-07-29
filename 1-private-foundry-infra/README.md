# 1 — Private Foundry infrastructure (Bicep & Terraform)

Infrastructure to stand up a **network-isolated Azure AI Foundry** (account +
project + supporting data resources) behind a **VNet with private endpoints**, so
agents run with **public network access disabled**. Two IaC options are provided
side by side — pick the one that matches your tooling.

| Path | Tool | What it does | Use when |
|---|---|---|---|
| [`bicep/`](bicep/) | **Bicep / ARM** | Full **end-to-end network isolation**: VNet, subnets, private endpoints + DNS, storage, Cosmos, AI Search, the Foundry **account + project**, role assignments, and the **capability host**. | You want to provision the **whole private Foundry stack** from scratch. |
| [`terraform/`](terraform/) | **Terraform / AzAPI** | Adds a **governed, agent-ready project** to an **existing** Foundry account: App Insights (tracing/token usage), an optional model deployment, and Entra-group RBAC. | You already have a Foundry **account** and want a project + governance via Terraform. |

> Reference walkthrough of the Bicep stack (layers, parameters, capability host):
> [`INFRA_PRIVATE_FOUNDRY.md`](INFRA_PRIVATE_FOUNDRY.md).

## Bicep quickstart

```powershell
# from bicep/
az login
az group create -n <your-resource-group> -l <your-region>
az deployment group create `
  -g <your-resource-group> `
  --template-file main.bicep `
  --parameters main.bicepparam
```

Fill in `main.bicepparam` (and `add-project.bicepparam`) with your own names
first. Capability-host helper scripts: [`bicep/createCapHost.sh`](bicep/createCapHost.sh)
and [`bicep/deleteCapHost.sh`](bicep/deleteCapHost.sh).

## Terraform quickstart

```powershell
# from terraform/
Copy-Item terraform.tfvars.example terraform.tfvars   # then edit
az login
$env:ARM_SUBSCRIPTION_ID = "<your-subscription-id>"
terraform init
terraform plan  -out tf.plan
terraform apply tf.plan
```

See [`terraform/README.md`](terraform/README.md) for the full input reference and
the dev/prod example var files.

---

All names and IDs are **placeholders** — replace `<your-...>` values with your
own. State files, `.terraform/`, and real `*.tfvars` are git-ignored.
