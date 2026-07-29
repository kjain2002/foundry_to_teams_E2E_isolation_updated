"""Environment-driven configuration for the hosted-agent chat app.

Single source of truth for the hosted-agent / Code-Interpreter / container
behaviour. Loaded once, validated, exposed as a typed singleton.

This module is intentionally self-contained: it has NO dependency on the
sibling ``streamlit_app`` publisher or on the read-only ``publish-agent`` tree.
No secrets are hard-coded — everything comes from the environment / ``.env``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()


ContainerMode = Literal["auto", "explicit", "off"]


class HostedAgentConfig(BaseModel):
    """Typed view over the hosted-agent app's environment variables."""

    # --- Auth mode -----------------------------------------------------------
    # local = DefaultAzureCredential (your `az login` session)
    # sso   = Container Apps Easy Auth + on-behalf-of
    auth_mode: Literal["sso", "local"] = "local"
    entra_tenant_id: str | None = None
    entra_client_id: str | None = None
    entra_client_secret: str | None = None

    # --- Foundry target ------------------------------------------------------
    # e.g. https://<resource>.services.ai.azure.com/api/projects/<project>
    foundry_project_endpoint: str | None = None
    # Deployed hosted-agent name (Prompt/hosted agent — the identifier IS the name)
    hosted_agent_name: str | None = None
    # Optional explicit override of the full Responses URL. When unset it is
    # derived from foundry_project_endpoint + hosted_agent_name.
    agent_responses_url: str | None = None
    # Model id (informational / for logging; the hosted agent owns its model).
    model: str | None = None
    # Token scope (audience) for the Foundry data plane.
    foundry_scope: str = "https://ai.azure.com/.default"
    # Preview api-version used for the OpenAI v1 containers surface.
    containers_api_version: str = "2025-04-01-preview"

    # --- Container / Code Interpreter behaviour ------------------------------
    # auto     = create a fresh client-owned container per session on demand
    # explicit = always use container_id (must be provided)
    # off      = do not manage a container (rely on agent defaults)
    container_mode: ContainerMode = "auto"
    explicit_container_id: str | None = None
    # If true, prepend the `USE_CONTAINER_ID=...` hint to the user message so
    # the hosted agent binds Code Interpreter to the client-created container.
    send_container_hint: bool = True
    # If true, also pass a code_interpreter tool bound to the container id in the
    # Responses request `tools` array (belt-and-suspenders with the hint).
    bind_code_interpreter_tool: bool = True

    # --- Tool identity -------------------------------------------------------
    # MCP server label used to recognise MCP tool function calls in the
    # Responses output. Matched case-insensitively against item name/server_label.
    mcp_server_label: str = "mcp"

    # --- HTTP behaviour ------------------------------------------------------
    request_timeout_seconds: float = 120.0
    # Extra CA bundle path for private-endpoint / corporate proxy scenarios.
    ca_bundle_path: str | None = None
    # Set false only for isolated local testing behind a broken TLS proxy.
    verify_tls: bool = True

    # --- Upload guard rails --------------------------------------------------
    max_template_bytes: int = 25 * 1024 * 1024  # 25 MiB

    # ------------------------------------------------------------------ helpers
    @property
    def responses_url(self) -> str:
        """Fully-qualified Responses endpoint for the hosted agent."""
        if self.agent_responses_url:
            return self.agent_responses_url
        if not (self.foundry_project_endpoint and self.hosted_agent_name):
            raise RuntimeError(
                "Set AGENT_RESPONSES_URL, or both FOUNDRY_PROJECT_ENDPOINT and "
                "HOSTED_AGENT_NAME, to reach the hosted agent."
            )
        base = self.foundry_project_endpoint.rstrip("/")
        return (
            f"{base}/agents/{self.hosted_agent_name}"
            "/endpoint/protocols/openai/responses"
        )

    @property
    def containers_url(self) -> str:
        """Base URL for the project-scoped OpenAI v1 containers surface."""
        if not self.foundry_project_endpoint:
            raise RuntimeError(
                "FOUNDRY_PROJECT_ENDPOINT is required to manage Code Interpreter "
                "containers."
            )
        return f"{self.foundry_project_endpoint.rstrip('/')}/openai/v1/containers"


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_config() -> HostedAgentConfig:
    cfg = HostedAgentConfig(
        auth_mode=os.getenv("AUTH_MODE", "local"),
        entra_tenant_id=os.getenv("ENTRA_TENANT_ID"),
        entra_client_id=os.getenv("ENTRA_CLIENT_ID"),
        entra_client_secret=os.getenv("ENTRA_CLIENT_SECRET"),
        foundry_project_endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT"),
        hosted_agent_name=os.getenv("HOSTED_AGENT_NAME"),
        agent_responses_url=os.getenv("AGENT_RESPONSES_URL"),
        model=os.getenv("MODEL"),
        foundry_scope=os.getenv("FOUNDRY_SCOPE", "https://ai.azure.com/.default"),
        containers_api_version=os.getenv(
            "CONTAINERS_API_VERSION", "2025-04-01-preview"
        ),
        container_mode=os.getenv("CONTAINER_MODE", "auto"),  # type: ignore[arg-type]
        explicit_container_id=os.getenv("EXPLICIT_CONTAINER_ID"),
        send_container_hint=_as_bool(os.getenv("SEND_CONTAINER_HINT"), True),
        bind_code_interpreter_tool=_as_bool(
            os.getenv("BIND_CODE_INTERPRETER_TOOL"), True
        ),
        mcp_server_label=os.getenv("MCP_SERVER_LABEL", "mcp"),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
        ca_bundle_path=os.getenv("CA_BUNDLE_PATH"),
        verify_tls=_as_bool(os.getenv("VERIFY_TLS"), True),
        max_template_bytes=int(
            os.getenv("MAX_TEMPLATE_BYTES", str(25 * 1024 * 1024))
        ),
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

    if cfg.container_mode == "explicit" and not cfg.explicit_container_id:
        raise RuntimeError(
            "CONTAINER_MODE=explicit requires EXPLICIT_CONTAINER_ID (cntr_...)."
        )
    return cfg
