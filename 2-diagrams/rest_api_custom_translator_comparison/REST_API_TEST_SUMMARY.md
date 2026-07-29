# Native REST API Publish — Test Summary (Client)

**Purpose:** Validate the official Microsoft Foundry REST API path for publishing a
privately-networked Foundry agent to Microsoft Teams and Microsoft 365 Copilot — the
programmatic equivalent of the portal's *Publish to Teams* button — and confirm what it
does and does not support versus our custom translator bridge.

Reference: [Publish agents to Microsoft 365 and Teams by using the REST API](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network)

---

## What we tested

We published **two agents** through the native REST API against the private Foundry
project (`foundry64dp` / `project64dp`, public network access disabled):

| Agent | Capability | Why we chose it |
|---|---|---|
| Simple grounded agent (Azure AI Search) | Answers from an indexed document set | Represents the "standard" agent that should work cleanly |
| Starburst MCP agent | Live data via an MCP tool (OAuth / on-behalf-of the user) | Stress-tests delegated-token and tool-consent behavior |

To avoid any impact on agents already published to Teams, we tested against **fresh
copies** of both agents. Nothing in the existing production setup was modified.

---

## How we tested it (high level)

The native REST flow follows five steps. We executed all of them programmatically:

1. **Identity** — retrieved each agent's identity and the tenant ID.
2. **Bot Service** — created an Azure Bot Service resource per agent, with the Microsoft
   Teams channel enabled, pointing at the agent's activity endpoint.
3. **Protocol + authorization** — enabled the agent's *activity* protocol and the
   Bot Service authorization scheme.
4. **Publish** — called the Microsoft 365 publish API with the Bot Service resource ID
   and `Shared` scope. The API returned a Teams app ID and catalog title ID for each
   agent, confirming a successful publish.
5. **Controlled ingress** — because the Foundry project is private, Microsoft's channel
   adapters cannot reach it directly. We routed inbound Teams traffic through our
   existing controlled public entry point, which terminates TLS and validates the
   Microsoft-signed bot token before forwarding to the private Foundry endpoint. All
   changes here were **additive** — existing published agents kept working unchanged.

We then confirmed the inbound path was live and enforcing token validation before any
end-user test in Teams.

---

## Results

- **Publishing worked.** Both agents were accepted by the Microsoft 365 publish API and
  registered in the Teams / Copilot catalog under the publisher's own agents (`Shared`
  scope, no admin approval required).
- **Inbound path verified.** The controlled ingress correctly accepts the route and
  rejects unauthenticated calls, proving the plumbing from Microsoft's channel adapter
  through to the private Foundry endpoint is in place.
- **Simple agent behaves as expected** for standard question-and-answer over indexed
  content.
- **MCP agent exposes the known limitation.** Delegated, per-user tool authentication
  (OAuth / on-behalf-of) and interactive tool consent are **not** carried through the
  native published path the way they are in our custom bridge. This matches Microsoft's
  own documented runtime behavior ("MCP approval required"). In short: the native path is
  excellent for standard and search-grounded agents, but tools that require per-user
  delegated consent are where it falls short today.

---

## Takeaway for the decision

- Choose the **native REST API publish** when the goal is a standard catalog experience
  with the smallest possible integration footprint — Foundry owns the publishing and
  activity-protocol experience.
- Choose the **custom translator bridge** when the agent depends on per-user delegated
  tokens or interactive tool consent (for example, the Starburst MCP scenario), where we
  need to own the token handling and Teams behavior.

Both approaches reach the *same* private Foundry agent; the difference is who owns the
Teams-facing integration and how much delegated-token control is required.
