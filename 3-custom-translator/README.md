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
- [`policies/`](policies/) — APIM policy XML. [`manifest/`](manifest/) — Teams app package. [`setup/`](setup/) — one-time platform setup.
- [`DESIGN_v1_per-bot-container.md`](DESIGN_v1_per-bot-container.md) / [`DESIGN_v2_shared-router-and-preauth.md`](DESIGN_v2_shared-router-and-preauth.md) — the design rationale.
- Diagrams: [`../2-diagrams/`](../2-diagrams/).
</details>

## File map
<details><summary>Full folder & file hierarchy (click to expand)</summary>

<details><summary><code>&lt;root&gt;</code> — platform infra, publish driver & design docs</summary>

- `bootstrap.bicep` / `bootstrap.bicepparam` — one-time APIM bridge (External VNet mode) so Bot Service reaches Foundry's private endpoint. Skip if you already have an APIM fronting Foundry.
- `bootstrap-translator.bicep` / `bootstrap-translator.bicepparam` — one-time shared infra every container depends on: ACR, Log Analytics, Container Apps env, storage + PE, and the `translator-mi` managed identity with RBAC.
- `publish-agent.bicep` — per-agent wiring: creates the Azure Bot + Teams channel + APIM operation routing to one Foundry agent (Entra app created by the driver, not Bicep).
- `publish-agent.ps1` — the shared publish driver (Entra app → KV secret → deploy `publish-agent.bicep` → render manifest → `out/<bot>.zip`).
- `setup_shared_apim_policies.ps1` / `setup_shared_apim_routes.ps1` — apply the shared APIM base policy and per-agent routes.
- `DESIGN_v1_per-bot-container.md` / `DESIGN_v2_shared-router-and-preauth.md` — design rationale (v1 per-bot container → v2 shared router + pre-auth).
- `README.md` — this file.
</details>

<details><summary><code>translator/</code> — the shared Bot ⇄ Foundry container</summary>

- `Dockerfile` — container image build.
- `requirements.txt` — Python deps.
- `README.md` — container-specific docs (env vars, local run, build & push).
- <details><summary><code>app/</code> — FastAPI/aiohttp application</summary>

  - `main.py` — entrypoint, `BotFrameworkAdapter`, `POST /api/messages` handler.
  - `foundry.py` — Foundry Agents API wrapper (thread → message → run → poll → reply).
  - `oauth.py` — per-user OBO token handling (Identity Passthrough).
  - `registry.py` — agent registry lookup (which agent a bot maps to).
  - `state.py` — conversationId → threadId store (Azure Table or in-memory).
  - `config.py` — env-driven settings.
  - `attachments.py` — Bot Framework attachment handling.
  - `__init__.py` — package marker.
  </details>
</details>

<details><summary><code>streamlit_app/</code> — click-to-publish UI (publish approach 3a)</summary>

- `app.py` — Streamlit UI + step machine (sign in → sub → account → project → agent → publish → download `.zip`).
- `auth.py` — Easy Auth header parsing + OBO / local `DefaultAzureCredential`.
- `config.py` — pydantic env loader. `discovery.py` — ARM REST: list subs / accounts / projects / agents.
- `publisher.py` — Graph + ARM + KV + ACR build + APIM policy rewrite + Teams `.zip`.
- `probe_agents.py` — helper to enumerate agents. `Dockerfile` — container image.
- `rebuild-arm.ps1` — recompile `../publish-agent.bicep` → `arm/*.json`. `restart.ps1` — dev restart helper.
- `requirements.txt` / `.env.example` — deps & config template.
- <details><summary><code>arm/</code></summary>

  - `publish-agent.json` — ARM template compiled from `../publish-agent.bicep` at build time.
  </details>
</details>

<details><summary><code>notebook_publisher/</code> — publish approach 3b</summary>

- `publish-agent-via-shared-container.ipynb` — every publish step in code, cell by cell.
</details>

<details><summary><code>manual_cli/</code> — publish approach 3c</summary>

- `README.md` — how to run `../publish-agent.ps1` directly for scripted / CI publishing.
</details>

<details><summary><code>policies/</code> — APIM policy XML</summary>

- `api-base.xml` — API-level base policy (JWT validation, audiences).
- `operation-per-agent.xml` — per-agent operation policy (native routing).
- `operation-per-agent-translator.xml` — per-agent operation policy routing to the translator container.
</details>

<details><summary><code>setup/</code> — one-time platform setup</summary>

- `SETUP_SHARED_PLATFORM.md` — deploy the shared platform once (APIM + container infra).
- `bootstrap_entra_apps.ps1` — create the two shared Entra apps (MCP resource app + OAuth client) and grant consent.
</details>

<details><summary><code>manifest/</code> & <code>manifest-test/</code> — Teams app package</summary>

- `manifest/` — `manifest.template.json` + `color.png` / `outline.png` icons (rendered per agent at publish time).
- `manifest-test/` — a concrete built sample: `manifest.json`, icons, and a ready `*.zip`.
</details>

</details>

*Replace every `<your-...>` placeholder; real `.env` and build artifacts are git-ignored.*
