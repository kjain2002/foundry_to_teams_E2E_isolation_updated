# Private Foundry → Microsoft Teams (end-to-end, network-isolated)

Publish **privately-networked Azure AI Foundry agents** to **Microsoft Teams**
— including agents that use **MCP tools requiring per-user (on-behalf-of)
auth** — without exposing Foundry to the public internet.

The repo covers three publishing routes with increasing customization (and
increasing complexity), plus the infrastructure and diagrams that explain the
private-networking model underneath.

> The Foundry portal's built-in *Publish to Teams* button does not work for
> Foundry accounts with public network access disabled. Every path here works
> against a **private** Foundry endpoint.

---

## What's in this repo

| Folder | Purpose |
|---|---|
| [`1-private-foundry-infra/`](1-private-foundry-infra/) | Stand up a **network-isolated Foundry** (account, project, VNet, private endpoints, capability host). **Bicep** and **Terraform** options. Start here if you don't already have a private Foundry. |
| [`2-diagrams/`](2-diagrams/) | Architecture, traffic-flow and identity/boundary diagrams (HTML). Read first to understand the moving parts. |
| [`3-custom-translator/`](3-custom-translator/) | Publish via a **shared Container App** that translates Bot Framework ⇄ Foundry Responses. The only path with a **public bot endpoint you own**, so it's the deterministic pick for cross-tenant delivery and for MCP tools that need per-user OBO. Three drivers: Streamlit UI, notebook, CLI. |
| [`4-native-rest-api-APIM-routing/`](4-native-rest-api-APIM-routing/) | Publish via Foundry's **native Activity Protocol** — but with **APIM** in front of it as the Bot messagingEndpoint. Use when you can't rely on Foundry's service-managed public route (or already run APIM for policy reasons). |
| [`5-native-rest-api-activity-protocol-routing/`](5-native-rest-api-activity-protocol-routing/) | Same result as folder 4 but **without APIM** — flip `enable_m365_public_endpoint` and let Foundry expose an IP-filtered public route itself. The simplest, docs-exact path when you don't need custom UX. |
| [`6-native-BYO-activity-protocol-echo/`](6-native-BYO-activity-protocol-echo/) | Deploy your **own** hosted agent that speaks Activity Protocol natively, sitting as a helper front-door in front of an existing Foundry Responses agent. Only route that gives you customizable Teams UX (typing indicator, file consent, adaptive cards) and durable per-user state — but requires **sideloading a custom Teams manifest** if you want file support. |

---

## Which route should you pick?

The three "publish" folders (3, 4/5, 6) trade **simplicity** for **control**.
Pick based on what you actually need.

### At a glance

| Capability | 3 – custom translator | 4 – REST + APIM | 5 – REST, no APIM | 6 – BYO Activity |
|---|:---:|:---:|:---:|:---:|
| No custom compute per agent | ✅ (shared container) | ✅ | ✅ | ⚠ (one hosted agent per helper) |
| Foundry-generated Teams manifest (no sideload) | ✅ | ✅ | ✅ | ⚠ (Step 7 is Foundry publish, but files need Step 8 sideload) |
| Custom Teams UX (typing, file cards, adaptive cards) | ✅ | ❌ | ❌ | ✅ |
| File **reads** in Teams | ✅ (translator) | ❌ | ❌ | ✅ *only after sideload* |
| File **downloads** back to Teams | ✅ | ❌ | ❌ | ✅ *only after sideload* |
| Custom loading indicator | ✅ | ❌ | ❌ | ✅ |
| Preference / onboarding card | ✅ | ❌ | ❌ | ✅ |
| Durable per-user state | ✅ | ❌ | ❌ | ✅ |
| Per-user MCP tools (OBO) | ✅ | ❌ | ❌ | ✅ (via `x-ms-user-token` forward) |
| Cross-tenant delivery reliability | ✅ (public bot URL you own) | ⚠ same-tenant-reliable | ⚠ same-tenant-reliable | ⚠ same-tenant-reliable |
| Setup effort | High (one-time) | Medium | Low | Medium |

### Decision tree

- **You need Teams file uploads / downloads, and you can't sideload** →
  folder 3.
- **You need per-user MCP tools (OBO) that require the signed-in user's
  identity** → folder 3.
- **You need cross-tenant reliable delivery** → folder 3 (only path with a
  public bot endpoint you own).
- **You need custom Teams UX (loading, consent cards, preferences) AND you
  *can* sideload a custom Teams package** → folder 6.
- **You just want an agent in Teams, no bells or whistles, no APIM** →
  folder 5.
- **You have organizational reasons to keep APIM in front of the Bot** (rate
  limiting, policy) → folder 4.

---

## Route-by-route: what works, what doesn't

### 4 — REST + APIM ([details](4-native-rest-api-APIM-routing/README.md))

**What it does:** enables Foundry's native Activity Protocol on the agent,
puts APIM in front of it, points the Bot at the APIM URL, publishes via
Foundry's `microsoft365/publish` API.

- ✅ No custom compute per agent.
- ✅ Standard `az` CLI + REST calls; no container to run.
- ✅ APIM available for enterprise policy hooks (rate-limit, IP allow-list,
  audit) if you already run one.
- ❌ **No customizable Teams UX.** Foundry generates the Teams manifest
  server-side and hard-codes `supportsFiles: false` — files, consent cards,
  adaptive UI aren't reachable.
- ❌ **Same-tenant reliable only.** The Bot is single-tenant; users in
  other tenants may see intermittent silence.

### 5 — REST, no APIM ([details](5-native-rest-api-activity-protocol-routing/README.md))

**Everything folder 4 does, minus APIM.** You flip
`enable_m365_public_endpoint = true` on the agent and Foundry itself
exposes an IP-filtered public route Teams can reach.

- ✅ Same result as folder 4 with less infrastructure.
- ✅ Recommended default when you don't need APIM's policy features.
- ❌ Same UX limits: no `supportsFiles`, no custom cards, no typing
  indicator — same reason (Foundry's manifest is fixed).
- ❌ Same cross-tenant delivery caveat.

### 6 — BYO Activity Protocol ([details](6-native-BYO-activity-protocol-echo/README.md))

**Own the activity-protocol code.** You deploy a small hosted agent
(`agent-activity`) that speaks Activity Protocol itself; every message it
receives is forwarded to your existing Foundry Responses agent. You get to
render whatever you want in Teams:

- ✅ **Real Teams UX:** typing indicator, `FileConsentCard` for outbound
  files, preference/adaptive cards, custom welcome text.
- ✅ **File reads** (into the target agent's session sandbox) and **file
  downloads** (via OneDrive + `FileInfoCard`) — implemented and shipping in
  this folder's `sessions.py` and `outfiles.py`.
- ✅ **Durable per-user state** in Azure Blob (survives cold starts,
  redeploys, multiple replicas).
- ✅ Same **`RESET**`** semantics as consumer AI apps.
- ⚠ **Files only actually work after Step 8 sideload.** Publishing via
  Foundry's `microsoft365/publish` (Step 7) still generates a manifest with
  `supportsFiles: false` — the API has no way to override it. To turn files
  on you must install the custom Teams app package built by
  `build_package.py` (via sideload / org catalog / setup policy assigned to
  a group).
- ⚠ Same cross-tenant caveat as folders 4/5 — the Bot is single-tenant
  and the messagingEndpoint is a Foundry private URL. Same-tenant works
  every time; other-tenant is intermittent.
- ⚠ You now maintain the code inside `src/agent-activity/`.

---

## Typical end-to-end path

1. **Infra** — if you don't already have a private Foundry, deploy one from
   [`1-private-foundry-infra/`](1-private-foundry-infra/) (Bicep or Terraform).
2. **Understand the flow** — skim [`2-diagrams/`](2-diagrams/).
3. **Pick a route** using the decision tree above.
4. **Publish**:
   - Folder 3 → deploy the shared translator once, then wire per agent.
   - Folder 4 → follow `README.md` Steps 0–7.
   - Folder 5 → follow `README.md` Steps 1–6.
   - Folder 6 → follow `README.md` Steps 1–9.
5. **Install in Teams** — for folders 3/4/5 the app appears automatically
   via `microsoft365/publish`; for folder 6, sideload the custom package
   if you want file support.

---

## Requirements

Common to every folder:

- An Azure subscription with permission to create resources (Contributor on
  the RG at minimum; Owner if you plan to deploy folder 1's infrastructure
  or fold in observability role assignments).
- **Azure CLI** (`az`) 2.60+; for folders 6 also **Azure Developer CLI**
  (`azd`) 1.34+ with the `azure.ai.agents` extension.
- **Bicep** and/or **Terraform** for folder 1.
- A private Foundry account + project you can `az rest` against — folder 6
  additionally requires that project to allow you to **use an existing
  project** in `azure.yaml` (`ai-project.endpoint`).
- Network reachability to the project's private endpoint (VPN /
  ExpressRoute / VNet peer / bastion). Steps that PATCH agent config or
  call `microsoft365/publish` hit private endpoints.

Route-specific extras:

- Folder 3 → APIM instance + Container Apps environment + a storage table
  for the shared-router registry + a Streamlit / notebook runtime.
- Folder 4 → APIM instance.
- Folder 5 → optional Log Analytics + App Insights for observability.
- Folder 6 → a storage account for durable state; ability to sideload a
  Teams app package (personally or via admin) if you want file support.

## Roles / RBAC

The route READMEs list per-route roles in their **Prerequisites** section.
Common assignments used across the repo:

- **Foundry User** on the project (to `az rest` the agents API).
- **Azure Bot Service Contributor** on the resource group.
- **API Management Contributor** on APIM (folder 4).
- **Storage Blob Data Contributor** to the *agent's managed identity* on the
  state storage account (folder 6).
- **Monitoring Metrics Publisher** to the *project managed identity* on the
  App Insights instance (folder 5 observability).
- **Owner** (or Contributor + User Access Administrator) if you're the one
  granting the RBAC above.

## Configuration & secrets

- Every credential and identifier is supplied via `config.ps1` / `.env` /
  azd env files (git-ignored). Copy the `*.example` files, fill in your
  values.
- Placeholders such as `<your-subscription-id>`, `<your-foundry-account>`,
  `<your-project>` appear verbatim in every example config.
- Real state (Terraform state, `_tmp/`, generated zips, `.azure/*`) is
  git-ignored globally — see [`.gitignore`](.gitignore).

## License

See [`LICENSE`](LICENSE).
