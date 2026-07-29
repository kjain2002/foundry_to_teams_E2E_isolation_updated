# Permissions & Role Requirements — Running Notes

Living doc. We add rows as we discover role/permission/consent requirements while
testing the Private-Foundry-Agent → Teams flow (with MCP-with-auth) in a
**locked-down tenant** (`<your-tenant-id>`) with a standard user
(`<you@your-org.com>`, **no directory roles**).

---

## 1. The two independent gates (don't conflate them)

| Gate | Control plane | Governs | Bypass without admin? |
|---|---|---|---|
| **Q1 — Publish Teams app to a group** | **Teams admin** (Teams Admin Center) | Upload custom app to org catalog + assign to users/AD group | ❌ No owner-side bypass |
| **Q2 — MCP tool OAuth consent** | **Entra** (user/admin consent policy) | User authorizing the app/OBO scope | ✅ Yes — **pre-authorization** (resource owner) |

---

## 2. Tenant capability probe (read-only Graph)

| Capability | Value | Meaning |
|---|---|---|
| `allowedToCreateApps` | ✅ true | Can register Entra app regs myself |
| `allowedToCreateSecurityGroups` | ✅ true | Can create a security group (target for Teams publish) |
| Directory roles on my account | ⚠️ `[]` (none) | **Not an admin** — no Entra admin, no Teams admin |
| User consent policy | `microsoft-user-default-low` | Self-consent only for **classified-low** scopes |

---

## 3. Consent (Q2) — empirical + documented findings

| Finding | Status | Evidence |
|---|---|---|
| No-admin self-consent to a **custom** MCP scope | ❌ **BLOCKED** ("Need admin approval") | Empirical — authorize URL test 2026-07-09 |
| Reason | Custom scope is **unclassified**; `-low` policy only allows classified-low | [configure-user-consent](https://learn.microsoft.com/entra/identity/enterprise-apps/configure-user-consent) |
| Classifying a scope as low | Needs admin | [configure-permission-classifications](https://learn.microsoft.com/entra/identity/enterprise-apps/configure-permission-classifications) (App Admin / Cloud App Admin) |
| **Pre-authorization** (owner-side, no admin) | ✅ Works — skips consent prompt | [preAuthorizedApplication](https://learn.microsoft.com/graph/api/resources/preauthorizedapplication); applied & verified |
| Consent policy scope | **Tenant-wide** (all users), not per-user | [user-admin-consent-overview](https://learn.microsoft.com/entra/identity/enterprise-apps/user-admin-consent-overview) |
| Cross-tenant | Consuming tenant's OWN policy/admin governs their users | — |

### Roles that can grant admin consent (Q2, if a client member has one)

| Role | roleTemplateId |
|---|---|
| Global Administrator | `62e90394-69f5-4237-9190-012177145e10` |
| Application Administrator | `9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3` |
| Cloud Application Administrator | `158c047a-c907-4556-b7ef-446551a6b5f7` |

One admin grants consent **once** → covers all users in that tenant.

---

## 4. Teams publish to a group (Q1) — findings — **CONFIRMED needs Teams admin**

| Requirement | Role/setting needed | Status for FDPO/<you> |
|---|---|---|
| Upload custom app to **org catalog** | **Global or Teams Administrator** (`69091246-20e8-4a56-aa4d-066075b2a7a8`) | ❌ Blocked — no directory roles |
| Target app to **a specific group** | **App permission policy** (Teams admin) / app-centric management | ❌ Blocked — no Teams admin |
| User-submit a custom app | Any user CAN submit — but **an admin must approve** before org availability | Submit ok; publish needs admin |
| **Personal** sideload (just me) | App-setup-policy "Upload custom apps" toggle (admin-set, can be on for users) | ❓ Untested |
| Create the target **security group** | `allowedToCreateSecurityGroups` | ✅ Allowed |

**No owner-side bypass** (unlike Q2's pre-authorization) — group distribution is inherently admin-gated.

Confirming docs (explicit):
- *"With app permission policies, **Teams admin** control what apps are available… for **a specific group of users**."* — [app-policies](https://learn.microsoft.com/microsoftteams/app-policies)
- *"Custom app upload **by admin** — Global or Teams administrators"; "Custom app submission by user — available… **after an admin approves**."* — [manage custom apps](https://learn.microsoft.com/microsoftteams/teams-custom-app-policies-and-settings#understand-custom-apps-and-the-available-settings)
- *"Teams Administrator — Everything in the Microsoft Teams admin center."* — [using-admin-roles](https://learn.microsoft.com/microsoftteams/using-admin-roles)

---

## 5. App registrations (created / reused in FDPO)

| App | appId | Role in test | Notes |
|---|---|---|---|
| `<your-mcp-resource-app>` | `<your-mcp-resource-app-id>` | **Resource** (MCP audience / OBO) | Scope `user_impersonation` (`d398b739-…`), consentType=User. SP created 2026-07-09 |
| `<your-oauth-client>` | `<your-oauth-client-id>` | **Client** (bot / OAuth-code) | redirect `http://localhost:8000/oauth/callback`; delegated `user_impersonation`; **pre-authorized** on resource; SP created |

**Other resources created (FDPO, 2026-07-09):**
- Security group `<your-oauth-client>-group` — id `0106fe57-f085-4708-bbb9-fddba3476da5`; members: <you> (`840c1ed4-…`), JohnHenry Hain (`2a3bbb9c-…`), Brian Cheng (`d7e4d831-…`). ✅ **Created + members added with NO admin** (confirms `allowedToCreateSecurityGroups` + members can add existing users). Target for the Teams group-publish test. Note: corp users appear as `#EXT#` B2B Members in FDPO.
- Teams test package `publish-agent/manifest-test/<your-oauth-client>-teams-app.zip` (points at `<your-oauth-client>` bot).

---

## 6. Open items to test

- [x] **Teams Administrator role: CONFIRMED ABSENT** (directoryRoles = `[]`, 2026-07-09) → cannot publish to a group; needs a Teams admin (Route 2/3). *Caveat: shows only ACTIVE roles — PIM-eligible roles not checked.*
- [x] **FDPO Teams "Upload an app" shows ONLY "Submit an app to your org"** (2026-07-09) → **personal sideload is DISABLED in FDPO** (app-setup policy "Upload custom apps" = off). Earlier "personal sideload works" was a DIFFERENT tenant, NOT FDPO.
  → In FDPO, getting ANY custom Teams app to users (even yourself) needs a **Teams admin** to approve the submission or upload it. Only **Submit** is self-serve.
  → Boundary A *Teams* test in FDPO needs a one-time Teams-admin approval. (MCP consent/OBO can still be proven in the **public Foundry playground** — no Teams, no admin.)
- [ ] PIM eligible roles? (entra.microsoft.com → PIM → My roles → Eligible)
- [ ] Confirm "Submit an app to your org" actually accepts the submission (self-serve submit works?)
- [x] **Teams admin center access: DENIED** (INVALID_PRIVILEGE, 2026-07-09) — 3rd confirmation of no Teams admin. NOTE it opened the CORP tenant (org 72f988bf), not FDPO; irrelevant since no Teams admin in FDPO either.
- [ ] Pre-auth bypass end-to-end (re-run authorize URL → expect no "admin approval")

---

## 7. Prerequisite roles — check + remediation (least-privilege; no Global Admin needed)

| Task | Role you need | Citation | How to check you have it | If you DON'T have it (easiest → hardest) |
|---|---|---|---|---|
| **Q2 — MCP OAuth consent** (per-user token / OBO) | **Cloud Application Administrator** (Entra) | ["Application Administrator, or Cloud Application Administrator"](https://learn.microsoft.com/entra/identity/enterprise-apps/configure-permission-classifications) · [user/admin consent overview](https://learn.microsoft.com/entra/identity/enterprise-apps/user-admin-consent-overview) | `az rest --method get --url "https://graph.microsoft.com/v1.0/me/memberOf/microsoft.graph.directoryRole?$select=displayName,roleTemplateId"` → look for `158c047a-…` (Cloud App Admin) / `9b895d92-…` (App Admin) / `62e90394-…` (Global). Or [entra.microsoft.com](https://entra.microsoft.com) → Roles & administrators → My roles | **Each item below is a separate route:**<br><br>**Route 1 — easiest, no admin:** Pre-authorize the client on the resource app (you own it — what we did).<br><br>**Route 2:** Ask any Cloud App Admin to click **Grant admin consent** once (covers all users).<br><br>**Route 3:** Ask admin to **classify the scope as low** → users then self-consent.<br><br>**Route 4:** Request Cloud App Admin via PIM. |
| **Q1 — publish Teams app to a group** | **Teams Administrator** (Teams) | ["Custom app upload by admin — Global or Teams administrators"](https://learn.microsoft.com/microsoftteams/teams-custom-app-policies-and-settings#understand-custom-apps-and-the-available-settings) · [app permission policies control group access](https://learn.microsoft.com/microsoftteams/app-policies) | `az rest … /me/memberOf/microsoft.graph.directoryRole` → look for `69091246-20e8-4a56-aa4d-066075b2a7a8` (Teams Administrator). Or open [admin.teams.microsoft.com](https://admin.teams.microsoft.com) → Teams apps → Manage apps → is **Upload** available? | **Each item below is a separate route:**<br><br>**Route 1 — easiest, no admin (testing only):** Personal sideload (if app-setup policy allows) — just you, *not* a group.<br><br>**Route 2:** Submit the app via Teams client → an admin approves.<br><br>**Route 3:** Ask a Teams Admin to upload + target the group via **app permission policy** / app-centric management.<br><br>**Route 4:** Request the Teams Administrator role.<br><br>*(No owner-side bypass exists for Q1.)* |
