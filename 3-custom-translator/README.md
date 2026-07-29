# 3 — Custom translator (shared container)

> Step-by-step guide to publishing agents to Teams through **one shared container**
> that translates Bot Framework ⇄ Foundry. Format: one-line summary per step →
> expand for details. Broad → narrow.

## Goal
Publish **any number** of private-network Foundry agents to Teams — including
agents whose **MCP tool must act as the signed-in user (per-user OBO)** — with
**no new compute per agent**. Deploy the platform once; then each agent is one
"publish" (a registry row + a Teams `.zip`).

## When to use this (vs. the REST API native channel)
Use this when a tool needs the **end user's identity**. If not, the simpler
[REST API native channel](../4-rest-api-native-channel/) may be enough.
<details><summary>details</summary>

- Shared **Container App** behind APIM translates Bot Framework ⇄ Foundry Responses API and forwards the per-user token.
- Adding an agent = **one registry row + Bot + Teams manifest** — no new container.
- Full comparison table: [root README](../README.md).
</details>

## Concept — the two auth boundaries
Boundary A = the user's identity reaches the MCP. Boundary B = the MCP uses it to
read data as the user.
<details><summary>details</summary>

- **Boundary A:** Foundry runs OAuth Identity Passthrough + OBO → your MCP gets a real per-user Entra JWT.
- **Boundary B:** works when your backend **accepts the Entra JWT**; a backend that can't must use a shared service account.
- Consent is granted **once org-wide** (pre-authorization / admin consent) — the *identity* is still per-user. Detail: [`DESIGN_v2_shared-router-and-preauth.md`](DESIGN_v2_shared-router-and-preauth.md).
</details>

---

## Step Log

**Step 1 — Deploy the shared platform once.** → [`setup/SETUP_SHARED_PLATFORM.md`](setup/SETUP_SHARED_PLATFORM.md)
<details><summary>details</summary>

- Reuse your private APIM + stand up **one** Container App and its infra (ACR, Container Apps env, storage, managed identity).
- The container code lives in [`translator/`](translator/); per-agent Bot + APIM operation are in [`publish-agent.bicep`](publish-agent.bicep) and [`policies/`](policies/).
</details>

**Step 2 — Create the two shared Entra apps and grant consent.** → [`setup/bootstrap_entra_apps.ps1`](setup/bootstrap_entra_apps.ps1)
<details><summary>details</summary>

- The MCP **resource app** (exposes the scope) + the **OAuth client**.
- **2a)** Pre-authorize the client on the resource app (no admin needed), **or**
- **2b)** Grant admin consent on the scope (needs Cloud Application Administrator).
</details>

**Step 3 — Point each agent's Foundry MCP tool at that client + scope.**
<details><summary>details</summary>

- In Foundry, set the MCP tool's auth to **OAuth Identity Passthrough → Custom** using the shared OAuth client + scope.
- Only needed for agents whose tool requires per-user auth. See [`setup/SETUP_SHARED_PLATFORM.md`](setup/SETUP_SHARED_PLATFORM.md).
</details>

**Step 4 — Publish each agent (pick one of three ways).**
<details><summary>details</summary>

- **3a — Streamlit UI** → [`streamlit_app/`](streamlit_app/): click-to-publish; best for non-developers.
- **3b — Notebook** → [`notebook_publisher/`](notebook_publisher/): every step in code.
- **3c — Manual CLI** → [`manual_cli/`](manual_cli/): runs [`publish-agent.ps1`](publish-agent.ps1) for scripted/CI.
- All three call the same tested `publisher.py` and produce the same result: Entra app + Azure Bot + Teams channel + registry row + a Teams `.zip`. **No new compute.**
</details>

**Step 5 — Install in Teams and test.**
<details><summary>details</summary>

- Upload the generated `.zip`, message the agent.
- If it uses a per-user tool, you'll see a consent/sign-in card once, then it answers **as the user**.
</details>

---

## What else is in this folder
<details><summary>details</summary>

- [`translator/`](translator/) — the shared Bot ⇄ Foundry container (FastAPI).
- [`hosted_agent_app/`](hosted_agent_app/) — optional hosted-agent chat & verify sample (Responses API + Code Interpreter + MCP tool).
- [`policies/`](policies/) — APIM policy XML. [`manifest/`](manifest/) — Teams app package. [`setup/`](setup/) — one-time platform setup.
- [`DESIGN_v1_per-bot-container.md`](DESIGN_v1_per-bot-container.md) / [`DESIGN_v2_shared-router-and-preauth.md`](DESIGN_v2_shared-router-and-preauth.md) — the design rationale.
- Diagrams: [`../2-diagrams/`](../2-diagrams/).
</details>

*Replace every `<your-...>` placeholder; real `.env` and build artifacts are git-ignored.*
