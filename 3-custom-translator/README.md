# 3 — Custom translator (shared container) → Teams

Publish **any number** of private-network Foundry agents to Microsoft Teams
through **one shared Container App** behind APIM — **with or without** an MCP
tool that needs **per-user (on-behalf-of) auth**.

> **TL;DR**
> - Deploy the shared platform **once** (APIM + one Container App + two Entra apps).
> - After that, **each new agent = one "publish"** → a registry row + a Teams `.zip`.
> - **No new compute per agent** — every agent shares the one container behind APIM.

Use this approach when your agent has a **tool that must act as the signed-in
user**. If you don't need per-user tool auth, the simpler
[native REST-API channel](../4-rest-api-native-channel/) may be all you need
(see the comparison table in the [root README](../README.md)).

---

## ⚖️ Per-agent compute vs. one shared container

| Dimension | Per-agent Function App | **Shared Container App (this folder)** |
|---|---|---|
| Both sit behind | APIM (private) | APIM (private) |
| New agent | Entra app + Bot + **Function App** | Entra app + Bot + **registry row** |
| Compute instances | **N Function Apps** | **1 shared container** |
| Scaling to many agents | N functions to deploy/patch | 1 container, N rows |
| Per-user token | ✅ (function mints & forwards) | ✅ (Foundry passthrough **or** bot-forward) |
| Cost | N always-on/consumption apps | 1 container + shared APIM |
| Publish UX | manual per agent | **UI / notebook / CLI** |

---

## 🚀 3 ways to publish an agent

All three produce the same result — an **Entra app + Azure Bot + Teams channel +
registry row + a Teams `.zip`** — and the UI and notebook both call the same
tested `publisher.py`, so they behave identically. Pick whichever fits.

| Option | Best for | Where |
|---|---|---|
| **3a — Streamlit UI** | click-to-publish, non-developers | [`streamlit_app/`](streamlit_app/) |
| **3b — Notebook** | see every step in code | [`notebook_publisher/`](notebook_publisher/) |
| **3c — Manual CLI** | scripted / CI, full control | [`manual_cli/`](manual_cli/) → runs [`publish-agent.ps1`](publish-agent.ps1) |

Supporting pieces shared by all three live at the root of this folder:
`translator/` (the Bot ⇄ Foundry service), `publish-agent.bicep` (per-agent Bot +
APIM operation), `policies/` (APIM policy XML), `manifest/` (Teams app package),
`setup/` (one-time shared-platform setup), and `hosted_agent_app/` (an optional
hosted-agent chat & verify sample).

---

## 🔐 The auth story — two boundaries

Know which boundary you're on:

- **Boundary A — the user's identity reaches the MCP tool.** Foundry runs
  *OAuth Identity Passthrough*, performs the OBO exchange, and calls your MCP with
  a **real, per-user Entra JWT**.
- **Boundary B — the MCP uses that identity to read data *as the user*.** This
  works when your backend **accepts the Entra JWT**. A backend that can't accept
  the Entra JWT must fall back to a **shared service account** for data access —
  a *backend* limitation, not an architecture gap.

### "True per-user consent" vs. org-wide pre-authorization

| | True per-user consent | What this repo uses |
|---|---|---|
| Who approves the scope | **each user** clicks the AAD consent screen | granted **once for the org** (pre-authorization, or admin consent) |
| Needs an admin? | depends on tenant policy | **no** — pre-auth is an app-owner action |
| Per-user **identity** to the data | ✅ | ✅ (still a real per-user OBO JWT) |
| Per-user **approval decision** | ✅ | ❌ (approved up front, org-wide) |

The *identity* flowing to your data is genuinely per-user (real OBO); only the
per-user *approval prompt* is skipped — which is the enterprise norm (admin
consent does the same). Full detail:
[`DESIGN_v2_shared-router-and-preauth.md`](DESIGN_v2_shared-router-and-preauth.md).

---

## 🧭 How to set it up (high-level)

> Notation: **`na) nb)`** = the options/branches at step **`n`** — pick one.

1. **Deploy the shared platform once** — APIM (private) + **one shared Container
   App** and its infra (ACR, Container Apps env, storage, managed identity).
   → [`setup/SETUP_SHARED_PLATFORM.md`](setup/SETUP_SHARED_PLATFORM.md).
2. **Create the two shared Entra apps** — the MCP **resource app** (exposes the
   scope) + the **OAuth client** — then grant consent:
   - **2a)** **Pre-authorize** the client on the resource app (no admin needed).
   - **2b)** *or* click **Grant admin consent** on the scope (needs the
     **Cloud Application Administrator** role).
   → [`setup/bootstrap_entra_apps.ps1`](setup/bootstrap_entra_apps.ps1).
3. **Point each agent's Foundry MCP tool** at that client + scope
   (**OAuth Identity Passthrough → Custom**). → [`setup/SETUP_SHARED_PLATFORM.md`](setup/SETUP_SHARED_PLATFORM.md).
4. **Publish each agent** — creates its **Entra app + Azure Bot + Teams channel +
   registry row** (+ a Teams `.zip`). **No new compute.** Pick **3a / 3b / 3c** above.
5. **Install in Teams + test** — upload the `.zip`, message the agent → consent
   card (if a per-user tool) → it answers **as the user**.

> Deep-dive on the design first? →
> [`DESIGN_v2_shared-router-and-preauth.md`](DESIGN_v2_shared-router-and-preauth.md).
> Don't have private Foundry yet? → [`../1-private-foundry-infra/`](../1-private-foundry-infra/).

---

## 🖼️ Diagrams worth your time ([`../2-diagrams/`](../2-diagrams/))

| Diagram | What it shows |
|---|---|
| `private_foundry_to_teams_architecture` | The whole picture: APIM + shared container + private Foundry. **Start here.** |
| `traffic_flow_teams_to_foundry` | A single Teams message's path to the agent and back. |
| `boundary_a_b_infographic` | **Boundary A vs B** + where consent/OBO happens. |
| `streamlit_publisher_flow` | What "publish" actually does under the hood. |

---

*An older per-bot design (one container per agent) is kept for reference in
[`DESIGN_v1_per-bot-container.md`](DESIGN_v1_per-bot-container.md).*
