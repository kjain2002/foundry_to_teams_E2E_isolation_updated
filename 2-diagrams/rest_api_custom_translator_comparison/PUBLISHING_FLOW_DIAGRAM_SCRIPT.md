# Client Walkthrough Script

Deck: [publishing_approaches_client_deck.html](publishing_approaches_client_deck.html)

Present the slides in this order: high-level choice, at-a-glance comparison, native REST publish, then shared-container publisher.

## 1. High-Level Choice

Diagram: [publishing_approaches_high_level.html](publishing_approaches_high_level.html)

Start with the common foundation: both approaches connect Teams to the same private Foundry agent. The decision is not about the agent; it is about who owns the Teams-facing integration.

On the left is our custom Teams bridge. Its runtime path is Teams to Bot Service to APIM to the shared router to Foundry. We own that bridge, which gives us flexibility for routing, Teams behavior, and user-token handling.

On the right is native Foundry publishing. Its runtime path is Teams or Copilot to the channel adapter to controlled ingress to Foundry. Foundry owns the publishing and activity-protocol experience, which reduces the custom integration footprint.

## 2. Comparison At a Glance

Diagram: [publishing_approaches_at_a_glance.html](publishing_approaches_at_a_glance.html)

This is the summarized decision table. It preserves each key comparison point from the detailed discussion.

Teams delivery differs: custom produces a Teams bot and app package; native publishes a catalog agent for Teams and Microsoft 365.

Bridge ownership differs: custom means we operate APIM, routing, and bot behavior; native means Foundry operates the activity protocol and catalog publication.

User-token control is strongest in the custom route, where delegated OAuth and consent can be tailored. Native uses Foundry's supported authorization and tool authentication model.

Distribution differs: custom is sideloaded or administered through Teams; native uses `Shared` visibility or `Tenant` publication after approval. The operational tradeoff is direct: custom has more components but more flexibility, while native has less runtime to own.

## 3. Native Foundry REST Publish

Diagram: [rest_api_publish_flow_private_foundry.html](rest_api_publish_flow_private_foundry.html)

This is the official Foundry REST API path. It replaces the portal publish button when the Foundry project uses private networking.

Before publishing, choose the tested agent, get its identity and tenant ID, create Bot Service with the Teams channel, set metadata and scope, then call the Microsoft 365 publish API with the Bot Service resource ID.

Foundry validates the request, enables the activity endpoint, configures authorization, packages the app, and registers the catalog entry. `Shared` uses Foundry RBAC; `Tenant` becomes organization-wide after Microsoft 365 admin approval.

At runtime, Teams or Copilot sends a signed activity through a Bot Channel Adapter and controlled public ingress before it reaches the private Foundry endpoint. Private networking still requires that public TLS ingress, because the channel adapter cannot call a private IP directly.

## 4. Shared Container Publisher

Diagram: [streamlit_publisher_flow_shared_container.html](streamlit_publisher_flow_shared_container.html)

This is our custom Teams publishing path. One shared Container App acts as the router, so publishing another agent adds configuration rather than compute.

The publisher selects the subscription, Foundry project, and agent, adds Teams branding, and selects Publish. The workflow creates the Entra bot identity and secret, configures Bot Service and Teams, adds the APIM audience, writes the bot-to-agent registry row, and produces a Teams app ZIP.

When a user chats, the request flows from Teams through Bot Service and APIM to the shared private router. The router finds the agent from the bot ID, calls Foundry privately, and sends the result back through the same path.

For delegated tools, the primary approach is Foundry OAuth Identity Passthrough, with an alternate router-managed OAuth mode when needed. The tradeoff is explicit: this path gives us custom Teams behavior, routing, and token control, but we own the bridge, policies, state, and package lifecycle.

## Close

Choose native REST publish when a standard catalog experience and a smaller operating footprint are the priority. Choose the shared-container bridge when custom Teams behavior or precise delegated-token control is the deciding requirement.