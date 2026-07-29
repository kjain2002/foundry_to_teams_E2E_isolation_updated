# Private Foundry → Microsoft Teams (end-to-end, network-isolated)

Publish **privately-networked Azure AI Foundry agents** to **Microsoft Teams** —
including agents that use **MCP tools requiring per-user (on-behalf-of) auth** —
without exposing Foundry to the public internet.

This repo gives you **two publishing approaches** (native REST API and a custom
translator) plus the **infrastructure** to stand up a private Foundry and the
**diagrams** that explain the traffic and identity flows. It supports both
**hosted agents** and **non-hosted (Responses API) agents**.

> The Foundry portal's built-in *Publish to Teams* button does not work for
> Foundry accounts with **public network access disabled**. Both approaches here
> route Teams traffic to a **private** Foundry endpoint through **APIM**.

---

## 📂 What's in this repo

| Folder | Purpose |
|---|---|
| [`1-private-foundry-infra/`](1-private-foundry-infra/) | Stand up a **network-isolated Foundry** (account, project, VNet, private endpoints, capability host). **Bicep** and **Terraform** options side by side. Start here if you don't already have a private Foundry. |
| [`2-diagrams/`](2-diagrams/) | Architecture, traffic-flow and identity/boundary diagrams (Excalidraw + HTML). Read these first to understand the moving parts. |
| [`3-custom-translator/`](3-custom-translator/) | Publish via a **shared container app** that translates Bot Framework ⇄ Foundry. Needed for **per-user delegated MCP tools**. Three ways to drive it: **3a** Streamlit UI, **3b** notebook, **3c** manual CLI. |
| [`4-rest-api-native-channel/`](4-rest-api-native-channel/) | Publish via Foundry's **native channel** (activity protocol) using plain **REST API** calls + APIM routing. The quickest path when you don't need per-user tool auth. |

---

## 🧭 Which publishing approach should I use?

There are two fundamentally different ways to get a private Foundry agent into
Teams. Pick based on **whether your agent's tools need the end user's identity**.

### REST API (native channel) vs. Custom translator — comparison

| Dimension | **REST API — native channel** ([`4-…`](4-rest-api-native-channel/)) | **Custom translator — shared container** ([`3-…`](3-custom-translator/)) |
|---|---|---|
| **How it works** | Foundry's own **activity-protocol** endpoint handles Bot Framework ⇄ agent; APIM routes the Teams/Bot traffic straight to Foundry. | A **container app** you deploy translates Bot Framework ⇄ Foundry **Responses API**; APIM routes to the container. |
| **Extra compute per agent** | **None** — Foundry hosts everything. | **None per agent** — one **shared** container serves all agents (add an agent = one registry row). |
| **Setup effort** | **Low** — a handful of REST calls + one APIM operation per agent. | **Higher** — deploy the shared platform once (APIM + container + 2 Entra apps), then publish per agent. |
| **Per-user delegated tools (OBO / MCP needing the user's token)** | ⚠️ **Not supported** — the native channel can't carry a per-user token into the tool. | ✅ **Supported** — the container runs the OAuth code flow and forwards `x-ms-user-token` (or lets Foundry do the OBO passthrough). |
| **Custom message handling** (adaptive cards, attachments, streaming shaping) | Whatever the native channel provides. | ✅ Fully customizable in the translator. |
| **Auth to Foundry** | Bot Service RBAC + Entra on the activity-protocol endpoint. | Bot Framework JWT validated at APIM; container calls Foundry over the private endpoint. |
| **Best for** | Simple agents, fastest time-to-Teams, no per-user tool consent. | Agents with **per-user MCP tools**, custom UX, or **many agents at scale**. |
| **Trade-off** | Least code, least control; blocked if a tool needs the user's identity. | More infrastructure up front; unlimited flexibility afterward. |

**Rule of thumb:**
- Your agent only grounds on data Foundry can reach under **its own** identity
  (AI Search, a service-account MCP) → **start with the REST API native channel**
  ([`4-…`](4-rest-api-native-channel/)).
- Your agent has an **MCP tool that must act as the signed-in user** (per-user
  OBO / delegated data access) → use the **custom translator**
  ([`3-…`](3-custom-translator/)).

---

## 🚀 Typical end-to-end path

1. **Infra** — if you don't already have a private Foundry, deploy one from
   [`1-private-foundry-infra/`](1-private-foundry-infra/) (**Bicep** or **Terraform**).
2. **Understand the flow** — skim [`2-diagrams/`](2-diagrams/).
3. **Pick a publishing approach** using the table above.
4. **Publish** an agent:
   - Native channel → [`4-rest-api-native-channel/`](4-rest-api-native-channel/).
   - Custom translator → [`3-custom-translator/`](3-custom-translator/) (UI / notebook / CLI).
5. **Install in Teams** — upload the generated app package, message the agent, verify.

---

## ✅ Requirements

- An Azure subscription with permission to create the resources in folder 1
  (or an existing private Foundry account + project).
- **Azure CLI** (`az`) and, for IaC, **Bicep** and/or **Terraform**.
- **APIM** (internal/private) fronting the Foundry endpoint — deployed by folder 1.
- Network access (VPN / private endpoint) to reach the private Foundry endpoints
  when running the publishers locally.

---

## 🔧 Configuration & secrets

- Every credential/ID is supplied via **parameters or `.env`** — nothing is
  hard-coded. Copy the provided `*.example` files and fill in your own values.
- Real `.env` files, Terraform state, compiled ARM, and build artifacts are
  **git-ignored**. Do not commit secrets.
- Replace placeholders such as `<your-subscription-id>`, `<your-resource-group>`,
  `<your-foundry-account>`, `<your-apim-name>`, `<your-tenant-id>` before running.

---

## 📄 License

See [`LICENSE`](LICENSE).
