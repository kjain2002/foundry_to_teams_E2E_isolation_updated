"""Authentication — Easy Auth + on-behalf-of (production) or DefaultAzureCredential (local).

Mirrors the pattern from the foundry-agent-publisher reference app:

    sso   - read user identity from Container Apps Easy Auth headers,
            then exchange the incoming token via on-behalf-of (OBO)
            for a downstream token (Foundry, ARM, Microsoft Graph).
    local - use DefaultAzureCredential (developer's `az login` session).

`get_token_provider()` returns a callable `(scope) -> str` that yields a
bearer token for the given scope.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import streamlit as st
from azure.core.credentials import AccessToken
from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential
from msal import ConfidentialClientApplication

from config import get_config

logger = logging.getLogger(__name__)

# ─── Scopes used by this app ────────────────────────────────────────────────
SCOPE_ARM = "https://management.azure.com/.default"
SCOPE_FOUNDRY = "https://ai.azure.com/.default"
SCOPE_GRAPH = "https://graph.microsoft.com/.default"


@dataclass(frozen=True)
class UserPrincipal:
    object_id: str
    name: str
    upn: str
    tenant_id: str

    @property
    def display(self) -> str:
        return f"{self.name} ({self.upn})"


TokenProvider = Callable[[str], str]


# ─── Easy Auth header parsing ───────────────────────────────────────────────


def _parse_easy_auth_headers() -> tuple[UserPrincipal | None, str | None]:
    """Parse Container Apps Easy Auth headers from the current request.

    Returns (principal, access_token) or (None, None) if not present.
    Streamlit exposes request headers via streamlit.context.headers (>=1.36).
    """
    try:
        headers = st.context.headers
    except Exception:
        return None, None
    if not headers:
        return None, None

    principal_b64 = headers.get("X-MS-CLIENT-PRINCIPAL")
    access_token = headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN")
    if not principal_b64:
        return None, None

    try:
        decoded = json.loads(base64.b64decode(principal_b64).decode("utf-8"))
        claims = {c["typ"]: c["val"] for c in decoded.get("claims", [])}
        principal = UserPrincipal(
            object_id=claims.get(
                "http://schemas.microsoft.com/identity/claims/objectidentifier", ""
            ),
            name=claims.get("name", claims.get("preferred_username", "Unknown")),
            upn=claims.get("preferred_username", claims.get("upn", "")),
            tenant_id=claims.get(
                "http://schemas.microsoft.com/identity/claims/tenantid", ""
            ),
        )
        return principal, access_token
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to parse Easy Auth principal: %s", exc)
        return None, None


# ─── Token providers ────────────────────────────────────────────────────────


class _LocalTokenProvider:
    """DefaultAzureCredential-backed provider for local dev.

    The shared token cache (VS Code / Office) often produces tokens whose
    `oid` claim is the user's *home tenant* OID — which won't match RBAC
    grants made against the user's guest OID in an external tenant (e.g. a
    guest user in an external / locked-down tenant). To avoid that, we put
    `AzureCliCredential` first in the chain so `az login` (which produces
    tokens with the correct guest OID) always wins, then fall back to the
    full default chain if `az` isn't available.
    """

    def __init__(self) -> None:
        self._cred = ChainedTokenCredential(
            AzureCliCredential(),
            DefaultAzureCredential(
                exclude_interactive_browser_credential=False,
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
            ),
        )

    def __call__(self, scope: str) -> str:
        token: AccessToken = self._cred.get_token(scope)
        return token.token


class _OboTokenProvider:
    """On-Behalf-Of flow: exchange the user's incoming token for a downstream token."""

    def __init__(self, user_assertion: str) -> None:
        cfg = get_config()
        authority = f"https://login.microsoftonline.com/{cfg.entra_tenant_id}"
        self._app = ConfidentialClientApplication(
            client_id=cfg.entra_client_id,
            client_credential=cfg.entra_client_secret,
            authority=authority,
        )
        self._user_assertion = user_assertion

    def __call__(self, scope: str) -> str:
        result = self._app.acquire_token_on_behalf_of(
            user_assertion=self._user_assertion,
            scopes=[scope],
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"OBO token exchange failed for scope {scope}: "
                f"{result.get('error_description', result)}"
            )
        return result["access_token"]


# ─── Public API ─────────────────────────────────────────────────────────────


def get_current_user() -> UserPrincipal | None:
    """Return the signed-in user, or None if not authenticated."""
    cfg = get_config()
    if cfg.auth_mode == "local":
        return UserPrincipal(
            object_id="local-dev",
            name="Local developer",
            upn="local@dev",
            tenant_id=cfg.entra_tenant_id or "local",
        )
    principal, _ = _parse_easy_auth_headers()
    return principal


def get_token_provider() -> TokenProvider:
    """Return a callable that yields tokens for downstream APIs."""
    cfg = get_config()
    if cfg.auth_mode == "local":
        return _LocalTokenProvider()
    _, access_token = _parse_easy_auth_headers()
    if not access_token:
        raise RuntimeError(
            "Easy Auth access token header (X-MS-TOKEN-AAD-ACCESS-TOKEN) not present. "
            "Verify Container Apps authentication is enabled and tokenStore is on."
        )
    return _OboTokenProvider(user_assertion=access_token)


class AzureCoreTokenAdapter:
    """Adapter so MSAL/OBO tokens look like azure-core TokenCredential.

    Used to pass user-scoped tokens into Azure SDK clients
    (AIProjectClient, etc.) which expect a credential with `.get_token(*scopes)`.
    """

    def __init__(self, provider: TokenProvider, default_scope: str) -> None:
        self._provider = provider
        self._default_scope = default_scope

    def get_token(self, *scopes: str, **_kwargs) -> AccessToken:
        scope = scopes[0] if scopes else self._default_scope
        token = self._provider(scope)
        # MSAL doesn't return expiry directly via OBO result here; assume 1h
        return AccessToken(token=token, expires_on=int(time.time()) + 3600)
