# Copyright (c) Microsoft. All rights reserved.

"""Environment-driven config for the Activity-protocol bridge agent."""

import os

# The Foundry project endpoint (https://<account>.services.ai.azure.com/api/projects/<project>).
FOUNDRY_PROJECT_ENDPOINT = os.environ.get(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://<your-foundry-account>.services.ai.azure.com/api/projects/<your-project>",
)

# The EXISTING deployed agent whose Responses endpoint we forward to (its brain
# already has your tools). We do NOT rebuild that brain here.
TARGET_AGENT_NAME = os.environ.get(
    "TARGET_AGENT_NAME", "<your-existing-agent-name>"
)

# Azure Bot OAuth connection (configured on the bot resource) that fronts Entra
# for the user sign-in. The SDK sends the SSO OAuthCard for this connection.
# Leave empty when not doing OBO.
OAUTH_CONNECTION_NAME = os.environ.get("OAUTH_CONNECTION_NAME", "")

# The MCP resource scope the user token must carry so Foundry's OBO reaches
# your MCP tool (e.g. api://<mcp-app-id>/user_impersonation).
MCP_USER_SCOPE = os.environ.get("MCP_USER_SCOPE", "")

# Optional separate OBO exchange connection; defaults to the sign-in connection.
OBO_CONNECTION_NAME = os.environ.get("OBO_CONNECTION_NAME", OAUTH_CONNECTION_NAME)

# Toolbox MCP endpoint - used to probe for the CONSENT_REQUIRED consent URL when
# your agent's MCP tool needs it. Optional.
TOOLBOX_MCP_ENDPOINT = os.environ.get("TOOLBOX_MCP_ENDPOINT", "")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Storage account (blob) for durable state (prefs, asked flag, history). The
# agent's managed identity needs Storage Blob Data Contributor on it.
STATE_STORAGE_ACCOUNT = os.environ.get("STATE_STORAGE_ACCOUNT", "<your-storage-account>")
