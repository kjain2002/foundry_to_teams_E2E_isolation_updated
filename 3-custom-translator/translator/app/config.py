"""Environment-driven config for the translator container."""
import os


class Settings:
    # --- Bot Framework / Entra (single-agent fallback; optional in shared-router mode) ---
    bot_app_id: str = os.environ.get("BOT_APP_ID", "")
    bot_app_password: str = os.environ.get("BOT_APP_PASSWORD", "")
    bot_app_tenant_id: str = os.environ.get("BOT_APP_TENANT_ID", "")
    # SingleTenant — Bot Framework auth uses tenanted issuer.
    bot_app_type: str = os.environ.get("BOT_APP_TYPE", "SingleTenant")

    # --- Foundry target ---
    foundry_project_endpoint: str = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    # For Prompt Agents the identifier IS the display name (no asst_xxx).
    # Optional in shared-router mode (the agent comes from the registry row).
    foundry_agent_name: str = os.environ.get("FOUNDRY_AGENT_NAME", "")
    # Preview api-version for the OpenAI v1 containers surface (Code Interpreter
    # file container used to ingest Teams attachments in private-network mode).
    containers_api_version: str = os.environ.get(
        "CONTAINERS_API_VERSION", "2025-04-01-preview"
    )

    # Toolbox MCP endpoint (e.g. Starburst). When set, the translator probes it on
    # a create_session failure to detect CONSENT_REQUIRED and surface the reconnect
    # link to the user on the spot. Leave empty to skip the probe.
    toolbox_mcp_endpoint: str = os.environ.get("TOOLBOX_MCP_ENDPOINT", "")

    # --- State (conversationId -> threadId) ---
    # Use Azure Table Storage; falls back to in-memory dict if unset.
    thread_table_url: str = os.environ.get("THREAD_TABLE_URL", "")
    thread_table_name: str = os.environ.get("THREAD_TABLE_NAME", "threads")

    # --- MCP OAuth (in-container Authorization-Code flow) ---
    # When enabled, the bot obtains the USER's token via its OWN auth-code flow
    # (/api/oauth/start + /api/oauth/callback) and forwards it to Foundry as
    # x-ms-user-token so MCP tools run as that user. No public Function App;
    # APIM stays on the inbound (Bot -> APIM -> container) leg.
    mcp_oauth_enabled: bool = os.environ.get("MCP_OAUTH_ENABLED", "false").lower() == "true"
    # OAuth client = this bot's Entra app (client id + secret). Defaults to the
    # bot app reg so client == bot (bot-mints-token pattern). Needs a client SECRET.
    oauth_client_id: str = os.environ.get("OAUTH_CLIENT_ID", "") or bot_app_id
    oauth_client_secret: str = os.environ.get("OAUTH_CLIENT_SECRET", "") or bot_app_password
    oauth_tenant_id: str = os.environ.get("OAUTH_TENANT_ID", "") or bot_app_tenant_id
    # Public base URL where THIS container's /api/oauth/callback is reachable
    # (the APIM/ingress URL), e.g. https://<apim>.azure-api.net
    oauth_redirect_base_url: str = os.environ.get("OAUTH_REDIRECT_BASE_URL", "")
    # MCP resource scope the user consents to (audience of x-ms-user-token),
    # e.g. api://<mcp-resource-app-id>/user_impersonation
    oauth_mcp_scope: str = os.environ.get("OAUTH_MCP_SCOPE", "")
    # Table (in the thread_table_url account) holding per-user tokens.
    user_token_table_name: str = os.environ.get("USER_TOKEN_TABLE_NAME", "usertokens")

    # --- Shared-router registry (one container -> many agents) ---
    # Source of per-agent rows (first non-empty wins). If all are empty the
    # container falls back to a single agent built from the BOT_* / OAUTH_* vars.
    agent_registry_json: str = os.environ.get("AGENT_REGISTRY_JSON", "")
    agent_registry_file: str = os.environ.get("AGENT_REGISTRY_FILE", "")
    agent_registry_table_name: str = os.environ.get("AGENT_REGISTRY_TABLE", "")

    # --- Runtime ---
    port: int = int(os.environ.get("PORT", "3978"))


settings = Settings()
