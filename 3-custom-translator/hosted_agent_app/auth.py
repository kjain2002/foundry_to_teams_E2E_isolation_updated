"""Authentication for the hosted-agent app.

Self-contained token provider. Mirrors the proven publisher pattern but does
not import from it:

    local - DefaultAzureCredential (developer `az login` session). CLI first so
            the guest-tenant OID wins over shared-cache home-tenant tokens.
    sso   - Container Apps Easy Auth headers + on-behalf-of exchange.

`get_token_provider()` returns a callable ``(scope) -> str`` yielding a bearer
token. `get_current_user()` returns the signed-in principal (or a local stub).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Callable

from azure.core.credentials import AccessToken
from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    DefaultAzureCredential,
)

from config import get_config

logger = logging.getLogger(__name__)

TokenProvider = Callable[[str], str]


@dataclass(frozen=True)
class UserPrincipal:
    object_id: str
    name: str
    upn: str
    tenant_id: str


def _headers() -> dict | None:
    """Return the current request headers if Streamlit exposes them."""
    try:
        import streamlit as st

        return st.context.headers  # type: ignore[return-value]
    except Exception:  # pragma: no cover - non-streamlit context
        return None


def _parse_easy_auth() -> tuple[UserPrincipal | None, str | None]:
    headers = _headers()
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


class _LocalTokenProvider:
    """DefaultAzureCredential-backed provider for local dev (CLI first)."""

    def __init__(self) -> None:
        self._cred = ChainedTokenCredential(
            AzureCliCredential(),
            DefaultAzureCredential(
                exclude_shared_token_cache_credential=True,
                exclude_visual_studio_code_credential=True,
            ),
        )

    def __call__(self, scope: str) -> str:
        token: AccessToken = self._cred.get_token(scope)
        return token.token


class _OboTokenProvider:
    """On-Behalf-Of: exchange the user's incoming token for a downstream one."""

    def __init__(self, user_assertion: str) -> None:
        from msal import ConfidentialClientApplication

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
            user_assertion=self._user_assertion, scopes=[scope]
        )
        if "access_token" not in result:
            raise RuntimeError(
                f"OBO token exchange failed for scope {scope}: "
                f"{result.get('error_description', result)}"
            )
        return result["access_token"]


def get_current_user() -> UserPrincipal | None:
    cfg = get_config()
    if cfg.auth_mode == "local":
        return UserPrincipal(
            object_id="local-dev",
            name="Local developer",
            upn="local@dev",
            tenant_id=cfg.entra_tenant_id or "local",
        )
    principal, _ = _parse_easy_auth()
    return principal


def get_token_provider() -> TokenProvider:
    cfg = get_config()
    if cfg.auth_mode == "local":
        return _LocalTokenProvider()
    _, access_token = _parse_easy_auth()
    if not access_token:
        raise RuntimeError(
            "Easy Auth access token header (X-MS-TOKEN-AAD-ACCESS-TOKEN) not "
            "present. Verify Container Apps authentication + tokenStore are on."
        )
    return _OboTokenProvider(user_assertion=access_token)
