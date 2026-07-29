# Publishing Flow Diagram Script

Use this script when presenting the two publishing options.

## 1. Shared Container Publisher

Diagram: [streamlit_publisher_flow_shared_container.html](streamlit_publisher_flow_shared_container.html)

### Opening

This is our custom Teams publishing path for a private Foundry agent. It uses one shared Container App as a router, so publishing another agent does not create more compute.

### What the publisher does

1. The publisher signs in and selects the subscription, Foundry project, and agent.
2. The publisher enters the Teams name and branding, then selects **Publish**.
3. The system creates a bot identity in Entra ID and stores its secret in Key Vault.
4. It creates an Azure Bot Service and enables the Teams channel.
5. It adds the bot to APIM's allowed audience list, so APIM accepts messages for that bot.
6. It writes a registry row that maps the bot ID to the Foundry agent and its authentication mode.
7. It creates a Teams app ZIP for upload or sideloading.

### What happens when a user chats

1. A Teams user sends a message to the bot.
2. Azure Bot Service sends the Bot Framework activity to APIM.
3. APIM validates the bot token and forwards the request to the shared private Container App.
4. The shared router uses the bot ID to find the right Foundry agent.
5. The router calls Foundry through the private endpoint and sends the answer back to Teams.

### Authentication point

For tools that run as the user, the normal path is Foundry OAuth Identity Passthrough. The router can show the Foundry consent link in Teams. It also has an alternate OAuth mode where the router obtains and forwards a user token.

### Key message

The shared router gives us control over the Teams experience, routing, and delegated authentication. The tradeoff is that we own the Bot Framework bridge, APIM policy, router, state, and Teams package.

---

## 2. Native Foundry REST Publish

Diagram: [rest_api_publish_flow_private_foundry.html](rest_api_publish_flow_private_foundry.html)

### Opening

This is the official Foundry REST API path. It replaces the portal publish button when the Foundry project uses private networking.

### What we do before publishing

1. Choose the tested Foundry agent and get its identity and tenant ID.
2. Create an Azure Bot Service with a Microsoft Teams channel.
3. Set the app metadata, such as name, description, icons, version, and publish scope.
4. Call the Foundry Microsoft 365 publish API with the Bot Service resource ID.

### What Foundry does during publishing

1. Foundry validates the agent, metadata, and app version.
2. Foundry enables the agent's activity protocol endpoint.
3. Foundry configures Bot Service authorization:
   - `Shared` uses Foundry RBAC.
   - `Tenant` allows tenant-wide use after Microsoft 365 admin approval.
4. Foundry builds the Teams app package and registers the agent in the Teams and Microsoft 365 catalog.

### What happens when a user chats

1. The user sends a message from Teams or Microsoft 365 Copilot.
2. The Microsoft Bot Channel Adapter sends a signed activity request.
3. A public ingress component, such as a firewall, application gateway, or reverse proxy, receives the request and terminates TLS.
4. That component routes the request to the private Foundry activity endpoint.
5. Foundry validates the request, authorizes the user, runs the agent, and returns the response through the same path.

### Key message

Foundry owns the activity protocol and catalog publication. This removes the custom router and Teams ZIP workflow, but private networking still needs public ingress and TLS because the channel adapter cannot call a private IP directly.

---

## Closing Comparison

- Use the shared-container route when you need custom Teams behavior or control of delegated user-token handling.
- Use the native REST route when a standard Teams and Microsoft 365 catalog agent is enough and you want less custom runtime to operate.