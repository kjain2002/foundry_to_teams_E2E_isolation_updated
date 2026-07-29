# `publish-agent/` — v2 design: Pre-auth consent + one shared router container

> Companion to [DESIGN_v1_per-bot-container.md](DESIGN_v1_per-bot-container.md). That file documents the original
> **per-bot** design (one Container App per published agent). This file
> documents the two changes we are making now:
>
> 1. **Pre-authorization** — get per-user MCP consent working **without any
>    tenant-admin role**.
> 2. **One shared router container** — replace the *per-bot* Container App (and
>    a *per-agent* Function App) with a **single** Container App that
>    routes every published agent, and supports **both** tool-auth styles.

---

## TL;DR — what changes vs DESIGN_v1 (per-bot)

| Thing | v1 per-bot (before) | v2 shared router (now) |
|---|---|---|
| Container Apps | **one per bot** | **one shared** router for all bots |
| Per-agent Function App (native workaround) | still implied | **removed** — the shared container does its job |
| Per-user MCP consent | needed admin consent | **pre-authorization** (owner-side, no admin) |
| App-identity tools (MI / service account) | worked | still work — unchanged path |
| Per-user delegated tools (any MCP needing the user's identity) | native publish couldn't carry the user token | shared container runs the **in-container OAuth-code flow** and forwards `x-ms-user-token` |
| Per-agent objects | Entra app + Bot Service + Teams channel + **container** + APIM op | Entra app + Bot Service + Teams channel + **registry row** + APIM op |

Shared, one-time: **APIM** + **Container Apps Environment** + **one router
Container App** + **ACR** + **MCP server**. Per agent: **1 Entra app + 1 Bot
Service + 1 Teams manifest + 1 registry row**.

---

## Update 1 — Pre-authorization (no-admin per-user consent)

### The problem
For a tool that needs the **end user's** identity (e.g. an MCP with
row-level security), the user must consent to the MCP resource scope
(`api://<mcp-app>/user_impersonation`). In a locked-down tenant we have
**no Entra admin role**, so:

- **User self-consent** is blocked by tenant policy (`microsoft-user-default-low`
  + unclassified scope).
- **Admin consent** needs **Cloud Application Administrator** — which we don't have.

### The fix — pre-authorize the client on the resource app
On the **MCP resource app**, add the **bot/OAuth client app** to
*Expose an API → Authorized client applications* for the
`user_impersonation` scope. That marks the client as **pre-consented**: when a
user signs in through the client, Entra treats consent as already granted and
**skips the consent prompt entirely** — no admin, no per-user consent, no policy
change.

This is an **app-owner** action (you own both app registrations), so it needs
**zero directory-admin privileges**.

```
MCP resource app  (api://<mcp-app-id>/user_impersonation)
   └── Authorized client applications
         └── <bot/OAuth client app id>   ← pre-authorized ⇒ consent auto-granted
```

Example:
`<your-mcp-resource-app>` pre-authorizes client `<your-oauth-client>` for
`user_impersonation`.

> **Note:** pre-auth removes the *consent* gate only. Publishing a Teams app to a
> **group** still needs **Teams Administrator** — that step is parked until an
> E5/admin tenant is available. The auth proof runs fine without it (Bot
> Framework Emulator, then a single admin-approved Teams submit).

---

## Update 2 — One shared router container

### Why one container is enough (any tool)
The container is a **translator**, not a tool host: it speaks the Bot Framework
**Activity Protocol** on `/api/messages` and calls the Foundry agent via the
Agents/Responses API. **Tools execute inside Foundry**, so the container is
tool-agnostic — exactly like Foundry's own managed translator was in the
public-network setting. One shared container therefore serves **any** agent with
**any** tools.

The **only** tool-specific concern is per-user OAuth: for a delegated tool the
container must know **which OAuth scope** to request. That is a **config value**
(a registry row), never a second container.

### The two flows the shared container supports

```mermaid
flowchart TD
    msg["incoming Activity<br/>(recipient.id = bot app id)"] --> reg{registry.resolve(botId)}
    reg -->|auth_mode = app| appf["foundry.chat(agent, text)<br/>container Managed Identity only"]
    reg -->|auth_mode = oauth| tok{user token cached?}
    tok -->|yes| oauthf["foundry.chat(agent, text, user_token)<br/>forwards x-ms-user-token"]
    tok -->|no| card["send sign-in HeroCard<br/>/api/oauth/start?state=botId|userId"]
    card --> cb["/api/oauth/callback<br/>exchange code, store token by (botId,userId)"]
```

- **Flow A — app identity** (`auth_mode: app`): tools use the container's
  managed identity or a shared service account (e.g. a backend's "basic" mode). No
  sign-in, no user token. This is the majority case and the native-publish
  equivalent.
- **Flow B — per-user delegated** (`auth_mode: oauth`): the container runs its
  **own OAuth authorization-code flow** (`/api/oauth/start` +
  `/api/oauth/callback`), gets *that user's* token, and forwards it to Foundry as
  `x-ms-user-token`. This is the **user-token fix**, done in-container so **APIM stays
  the only public endpoint and no per-agent Function App is needed**.

Forwarding the user token is harmless to app-auth tools (Foundry only hands it to
tools that want it), so an agent that **mixes** tool types just sets
`auth_mode: oauth` and both kinds of tools work in the same run.

### How the shared container makes this happen (4 mechanisms)

1. **Per-agent registry** keyed by the incoming bot app id
   (`turn.activity.recipient.id`). See [`translator/app/registry.py`](translator/app/registry.py).
2. **Multi-bot credential resolution** — one `CloudAdapter` **per bot**, built
   lazily from the registry, so each bot's JWT is validated with the correct
   audience and outbound calls are signed with the correct secret.
3. **Token store keyed by `(botId, userId)`** — a user's Agent-A token never
   collides with their Agent-B token (different scopes).
4. **OAuth routes carry agent context** — the `state` param is `botId|userId`,
   so the callback exchanges the code against the **right** client/scope and
   stores the token under the composite key.

### Registry row schema

Env JSON (`AGENT_REGISTRY_JSON`) or file (`AGENT_REGISTRY_FILE`):

```jsonc
{
  "agents": [
    {
      "botAppId":        "<bot Entra app id>",
      "botAppPassword":  "<bot client secret>",
      "botAppTenantId":  "<tenant id>",
      "botAppType":      "SingleTenant",          // or MultiTenant (cross-tenant)
      "foundryAgentName":"<your-agent>",
      "authMode":        "oauth",                 // "app" | "oauth"
      "oauth": {                                   // only when authMode = oauth
        "clientId":        "<oauth client app id>",
        "clientSecret":    "<oauth client secret>",
        "tenantId":        "<tenant id>",
        "scope":           "api://<mcp-app-id>/user_impersonation",
        "redirectBaseUrl": "https://<apim>.azure-api.net"
      }
    }
  ]
}
```

Azure Table (`AGENT_REGISTRY_TABLE`, partition `agent`, row key = bot app id):
columns `botAppPassword, botAppTenantId, botAppType, foundryAgentName, authMode,
oauthClientId, oauthClientSecret, oauthTenantId, oauthScope, oauthRedirectBaseUrl`.

**Backward compatible:** if none of `AGENT_REGISTRY_JSON` / `_FILE` / `_TABLE`
is set, the container synthesizes **one** agent from the existing `BOT_*` /
`OAUTH_*` env vars — so the Stage-1 single-agent deployment keeps working
untouched.

### Container env vars (shared router)

| Var | Purpose |
|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | shared Foundry project (all agents live here) |
| `THREAD_TABLE_URL` | Azure Table account for conversation + token + registry state |
| `AGENT_REGISTRY_JSON` \| `AGENT_REGISTRY_FILE` \| `AGENT_REGISTRY_TABLE` | source of per-agent rows |
| *(fallback)* `BOT_APP_ID` / `BOT_APP_PASSWORD` / `BOT_APP_TENANT_ID` / `FOUNDRY_AGENT_NAME` / `MCP_OAUTH_ENABLED` / `OAUTH_*` | single-agent Stage-1 mode |

---

## How this maps to a per-user delegated scenario

- The native workaround runs **one Function App per agent** (private, behind APIM) to do the user-token dance.
- Here, **one shared Container App** behind APIM does the same OAuth-code +
  `x-ms-user-token` forwarding for **every** agent — nothing public except APIM.
- A delegated MCP tool = `authMode: oauth` with the backend's
  `user_impersonation` scope. Adding another such agent = **one more
  registry row + Bot Service + Teams manifest**, no new compute.
- An example MCP tool can stand in to prove the delegated flow.

Reference Terraform shape (next phase):
**shared-infra module** (APIM, CAE, router Container App, ACR, MCP app + PE) +
**per-agent module** (Entra app, Bot Service, Teams manifest, registry row).

---

## Status

| Piece | State |
|---|---|
| Pre-authorization (no-admin consent) | ✅ applied (`<your-mcp-resource-app>` → `<your-oauth-client>`) |
| In-container OAuth-code flow (`oauth.py`, routes, token store) | ✅ implemented |
| Shared registry-driven router (`registry.py`, multi-bot adapters, `(bot,user)` keys) | ✅ implemented |
| Single-agent backward compatibility | ✅ preserved |
| **Foundry OAuth Identity Passthrough (`app` mode) → consent card in Teams** | ✅ **PROVEN end-to-end in Teams** (2026-07-12) |
| Boundary A (user → MCP, per-user OBO + JWT) | ✅ proven (example MCP tool, Custom passthrough) |
| Boundary B (MCP → data *as the user*) | ⛔ stubbed on a JWT-incapable trial backend (→ service account); native on any JWT-accepting backend |
| Teams **group** publish (needs Teams Admin) | ⛔ parked (E5 tenant) |
| Terraform modules (shared + per-agent) | ⏳ next phase |

---

# Concepts & honest limitations (read this before adopting)

This section explains, in plain terms: (1) the tenant limitation and what I built
around it, (2) `app` vs `oauth` mode, (3) the real OBO + JWT flow and the exact
kind of consent I *couldn't* get, and (4) the MCP `validate-jwt` I'm missing and
why that's fine for this trial.

## 1. The tenant limitation — what I built, and what I'd do with admin

**The limitation.** Our tenant gives me **no directory-admin roles** — no
*Cloud Application Administrator*, no *Global Admin*, no *Teams Administrator*.
Two things break as a result:

- **Consent.** For a delegated tool the user must consent to the MCP scope
  (`api://<mcp-app>/user_impersonation`). Tenant policy
  (`microsoft-user-default-low` + unclassified scope) **blocks user
  self-consent**, and **admin consent needs a role I don't have**. So a normal
  user simply hits *"Need admin approval"* and dead-ends.
- **Teams distribution.** Publishing a Teams app to a **group / org catalog**
  needs *Teams Administrator*. I can only sideload / "Submit to org".

**What I built instead — pre-authorization.** On the **MCP resource app**, under
*Expose an API → Authorized client applications*, I added the OAuth client
(`<your-oauth-client>`) for the `user_impersonation` scope. That marks the client as
**pre-consented**: Entra treats consent as already granted and **skips the
prompt** — no admin, no policy change. This is an **app-owner** action (I own both
registrations), so it needs **zero directory-admin privileges**.

**What I'd do with a normal/admin tenant.** I'd just click **"Grant admin
consent"** once on the MCP scope (Cloud App Admin). Functionally it's the *same
outcome* as pre-auth — the scope is consented **once for the whole org**, and no
user is ever prompted. Pre-authorization is simply the **no-admin substitute for
admin consent**. (For Teams, an admin would publish to the org catalog instead of
sideloading.)

> One-liner: *pre-auth = admin-consent-without-an-admin.* Same end state, granted
> by the app owner instead of a directory admin.

## 2. `app` vs `oauth` mode (container registry) — who fetches the user token

The registry row's `authMode` picks **which layer obtains the user's token** that
reaches the MCP. It is **orthogonal** to OBO/JWT/consent — both modes produce a
**real, per-user JWT** at the MCP.

```
app  mode (PROVEN):  Teams user → container (relay) → FOUNDRY fetches token
                     (OAuth Identity Passthrough + pre-auth, does the OBO,
                      returns the consent link, calls MCP as the user)

oauth mode (reserve): Teams user → BOT fetches token (Azure Bot OAuth / Teams SSO)
                     → forwards x-ms-user-token → Foundry/MCP consumes it
```

| | `app` mode | `oauth` mode |
|---|---|---|
| Who runs the OAuth | **Foundry** (passthrough) | **the bot/container** |
| User token to MCP | real, per-user | real, per-user |
| Consent surface | Foundry consent card (`consent_link`) | container sign-in card |
| We proved it | ✅ yes (example MCP tool) | code path exists, dormant |
| Use it when | tool = *OAuth Identity Passthrough* | bot must mint the token (a native `func_bot`, or a `validate-jwt` MCP, or silent Teams SSO) |

**Why `app` is our default:** we proved a delegated MCP tool with **OAuth Identity
Passthrough → Custom** (Foundry does the OBO using `<your-oauth-client>`). In `app`
mode the container is a dumb relay that just surfaces Foundry's consent card —
**one** sign-in, no double-prompt. `oauth` mode is held in reserve for the
"bot-supplies-the-token" cases; it's the *shape* of a native per-agent Function App,
but you don't need it if you adopt Foundry passthrough.

## 3. The OBO + JWT flow, and the per-user *consent* I couldn't get

**What actually happens (and it's real):**

```
User taps "Open consent"  ─ connection confirmation, no privilege needed
        │
        ▼
OAuth authorize → login.microsoftonline.com  (scope user_impersonation)
        │   <your-oauth-client> is PRE-AUTHORIZED on the resource
        ▼
No admin-consent wall → token issued          ← pre-auth cleared the gate
        │
        ▼
Foundry OBO-exchanges it → REAL Entra JWT scoped to the MCP,
carrying THE USER'S identity (oid/sub) → calls the MCP as that user
```

**Two different "consents" — don't conflate them:**

| "Consent" | What it is | In our flow |
|---|---|---|
| **Entra permission consent** | the *"grant these scopes?"* / *"Need admin approval"* screen | **suppressed** by pre-auth (granted once, org-wide) |
| **Connection confirmation** | the *"Confirmation required → Allow access"* anti-phishing page | the user **does** click this; it carries no privilege |

**The thing I could *not* get = per-user *consent*.** With pre-auth (or admin
consent), **no individual user ever approves the scope** — it's granted **once
for everyone**. So there is **no per-user consent decision**.

**But the OBO and the JWT *are* fully per-user.** The minted token carries the
**actual signed-in user's identity**; a different user gets a different token.
So per-user **identity** flows to the MCP — only the **approval** was granted up
front.

> Note on consent: losing per-user *consent* is the **enterprise norm** —
> admin consent also grants once for the whole org. What matters (per-user
> **identity** reaching the data layer) is preserved.

## 4. The MCP `validate-jwt` I'm missing — and why it's fine here

Foundry sends a **real per-user JWT** to the MCP, but I **deliberately left the
APIM `validate-jwt` policy off** and the MCP does **not** check or use the token.
Reason: **Boundary B is stubbed on a JWT-incapable trial backend.**

```
Foundry (OBO) ─real per-user JWT─► APIM (no validate-jwt) ─► your MCP server
                                                                 │  ← ignores JWT
                                                                 ▼
                                              trial backend, via a SERVICE ACCOUNT (basic)
```

- The trial backend **can't accept an Entra JWT**, so the MCP authenticates
  to it as a **shared service account**, not as the user. It has **nothing to
  do** with the user's token downstream.
- `validate-jwt` is what you add when the MCP **enforces per-user access** —
  reject unauthenticated callers *and* hand a verified identity to the backend.
  With a service-account backend that would be a gate with nothing behind it, so
  we left it off (turning it on with an "Unauthenticated" tool earlier just
  401'd everything for no benefit).

**A JWT-accepting backend is exactly where `validate-jwt` earns its place:**
such a backend **accepts the Entra JWT**, so that MCP **validates** the token and
calls the backend **as the user** — that's **Boundary B done for real**, the one
piece the trial backend can't demonstrate. It works with **either** `app` or `oauth` mode,
because both deliver a real per-user JWT to the MCP.

---

# Per-agent Function App vs the shared Container App — pros & cons

Both solve the same core problem (carry the per-user token to a private Foundry
agent and surface it in Teams). The difference is **topology and operations**.

| Dimension | **Per-agent Function App** (native, private behind APIM) | **One shared Container App behind APIM** (this repo) |
|---|---|---|
| Compute per agent | **one Function App each** (private, behind APIM) | **none** — one shared container |
| New agent = | new Function App (compute) + wiring | **one registry row** + Bot Service + Teams manifest — **no new compute** |
| Scaling to N agents | N Function Apps to deploy/patch | 1 container, N rows |
| Networking | each Function App must be reachable | private container; APIM is the single ingress |
| Auth token source | bot/function mints & forwards the token | **Foundry passthrough** (`app`) *or* bot-forward (`oauth`) — both supported |
| Consent (no admin) | same Entra problem | solved via **pre-authorization** |
| Per-user OBO + JWT | ✅ (its whole job) | ✅ proven (example MCP tool, `app` mode) |
| Boundary B (data as user) | ✅ native on a JWT-accepting backend | ✅ on a JWT-accepting backend; ⛔ trial backend only |
| Self-service publishing | manual per agent | **Streamlit app / notebook** writes the row + manifest |
| Secret & identity mgmt | per Function App | centralized (KV + registry, container MI) |
| IaC | per-agent | **shared-infra module + per-agent module** (Terraform, next phase) |
| Cost | N always-on/consumption apps | 1 container + shared APIM/CAE |
| Patch/deploy surface | N Function Apps | 1 container + 1 gateway |
| Migration effort | — | republish each agent (reuses bot app; **no manifest/identity churn**) |

**When a per-agent Function App still makes sense:** a **single** agent, or a hard
requirement that the **bot** (not Foundry) mint the token (e.g. a bespoke
`validate-jwt` MCP that only trusts a token minted by the function). For that
case our **`oauth` mode** mirrors that design on the shared container — so you get
the same behavior **without** a per-agent Function App.

**Net:** the shared container is a **strict superset** — it does everything the
Function App does (via `oauth` mode) **and** the simpler Foundry-passthrough path
(`app` mode), while collapsing N Function Apps into **one shared container + APIM**
and turning "add an agent" into "add a row."
