"""Environment configuration loader.

Single source of truth for env-var-driven config. Loaded once at startup,
validated, and exposed as a typed singleton.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


class AppConfig(BaseModel):
    """Typed view over the app's environment variables."""

    # Auth mode
    auth_mode: Literal["sso", "local"] = "sso"

    # Entra ID (required when auth_mode=sso)
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: str | None = None

    # Azure deployment scope (pre-configured per tenant)
    subscription_id: str | None = None
    resource_group: str | None = None

    # APIM (output of bootstrap.bicep — operator pre-configures these once)
    apim_name: str | None = None
    foundry_bot_api_name: str = "foundry-bot"

    # Key Vault to store bot client secrets in (one secret per bot).
    bot_secrets_keyvault_name: str | None = None

    # Translator (output of bootstrap-translator.bicep — operator pre-configures these once)
    translator_cae_name: str | None = None
    translator_cae_default_domain: str | None = None     # e.g. <random>.swedencentral.azurecontainerapps.io
    translator_acr_name: str | None = None               # short name, not loginServer
    translator_acr_login_server: str | None = None       # e.g. acrtranslatorabc.azurecr.io
    translator_mi_name: str | None = None                # short name of the user-assigned MI
    translator_mi_client_id: str | None = None
    translator_thread_table_url: str | None = None       # e.g. https://sttranslatorabc.table.core.windows.net/

    # Shared-router publishing (one Container App serves every agent).
    # The publisher writes one registry row per agent instead of deploying a
    # per-bot Container App. The shared router reads AGENT_REGISTRY_TABLE.
    agent_registry_table_name: str = "agents"
    # Default container registry auth mode. Proven model = `app` (Foundry OAuth
    # Identity Passthrough drives consent+OBO; the container just relays). Set
    # `oauth` only to opt into the bot-mints-and-forwards-token path.
    default_auth_mode: Literal["app", "oauth"] = "app"
    # MCP resource scope the container requests for per-user sign-in, e.g.
    # api://<mcp-app-id>/user_impersonation. Used as a fallback until the MCP
    # server advertises OAuth protected-resource metadata for auto-discovery.
    default_mcp_scope: str | None = None
    # ONE shared OAuth client (pre-authorized on the MCP resource app) reused by
    # every agent so no per-bot pre-authorization is required.
    shared_oauth_client_id: str | None = None
    shared_oauth_client_secret: str | None = None
    # Public base URL where the container's /api/oauth/callback is reachable
    # (the APIM gateway). Defaults to https://<apim>.azure-api.net if unset.
    oauth_redirect_base_url: str | None = None


@lru_cache
def get_config() -> AppConfig:
    cfg = AppConfig(
        auth_mode=os.getenv("AUTH_MODE", "sso"),
        entra_tenant_id=os.getenv("ENTRA_TENANT_ID"),
        entra_client_id=os.getenv("ENTRA_CLIENT_ID"),
        entra_client_secret=os.getenv("ENTRA_CLIENT_SECRET"),
        subscription_id=os.getenv("SUBSCRIPTION_ID"),
        resource_group=os.getenv("RESOURCE_GROUP"),
        apim_name=os.getenv("APIM_NAME"),
        foundry_bot_api_name=os.getenv("FOUNDRY_BOT_API_NAME", "foundry-bot"),
        bot_secrets_keyvault_name=os.getenv("BOT_SECRETS_KEYVAULT_NAME"),
        translator_cae_name=os.getenv("TRANSLATOR_CAE_NAME"),
        translator_cae_default_domain=os.getenv("TRANSLATOR_CAE_DEFAULT_DOMAIN"),
        translator_acr_name=os.getenv("TRANSLATOR_ACR_NAME"),
        translator_acr_login_server=os.getenv("TRANSLATOR_ACR_LOGIN_SERVER"),
        translator_mi_name=os.getenv("TRANSLATOR_MI_NAME"),
        translator_mi_client_id=os.getenv("TRANSLATOR_MI_CLIENT_ID"),
        translator_thread_table_url=os.getenv("TRANSLATOR_THREAD_TABLE_URL"),
        agent_registry_table_name=os.getenv("AGENT_REGISTRY_TABLE", "agents"),
        default_auth_mode=os.getenv("DEFAULT_AUTH_MODE", "app"),
        default_mcp_scope=os.getenv("DEFAULT_MCP_SCOPE"),
        shared_oauth_client_id=os.getenv("SHARED_OAUTH_CLIENT_ID"),
        shared_oauth_client_secret=os.getenv("SHARED_OAUTH_CLIENT_SECRET"),
        oauth_redirect_base_url=os.getenv("OAUTH_REDIRECT_BASE_URL"),
    )

    if cfg.auth_mode == "sso":
        missing = [
            name
            for name, val in {
                "ENTRA_TENANT_ID": cfg.entra_tenant_id,
                "ENTRA_CLIENT_ID": cfg.entra_client_id,
                "ENTRA_CLIENT_SECRET": cfg.entra_client_secret,
            }.items()
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"AUTH_MODE=sso but missing required env vars: {', '.join(missing)}. "
                "See .env.example."
            )
    return cfg
