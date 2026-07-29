"""In-container OAuth Authorization-Code flow for per-user MCP tokens.

The bot acts as the OAuth client: it sends the Teams user to Entra's authorize
endpoint (scoped to the MCP resource), receives the code on
``/api/oauth/callback``, and exchanges it for the USER's access token. That token
is later forwarded to Foundry as ``x-ms-user-token`` so MCP tools run as the user.

This replaces a public-Function-App detour: the same custom-bot-code logic
lives in the translator container, so APIM stays on the inbound leg and no public
Function App is needed. Sign-in surfaces as a plain openUrl HeroCard button ->
standard browser AAD login, which renders in Teams (unlike Foundry's inline
``oauth_consent_request`` that never rendered).
"""
from __future__ import annotations

import urllib.parse

import aiohttp

from .registry import OAuthConfig

_SCOPE_SUFFIX = "offline_access openid profile"


def _authority(cfg: OAuthConfig) -> str:
    return f"https://login.microsoftonline.com/{cfg.tenant_id}"


def _scope(cfg: OAuthConfig) -> str:
    return f"{cfg.scope} {_SCOPE_SUFFIX}".strip()


def redirect_uri(cfg: OAuthConfig) -> str:
    base = cfg.redirect_base_url.rstrip("/")
    return f"{base}/api/oauth/callback"


def build_authorize_url(cfg: OAuthConfig, state: str) -> str:
    params = {
        "client_id": cfg.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(cfg),
        "response_mode": "query",
        "scope": _scope(cfg),
        "state": state,
        "prompt": "select_account",
    }
    return f"{_authority(cfg)}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"


async def _token_request(cfg: OAuthConfig, data: dict) -> dict:
    url = f"{_authority(cfg)}/oauth2/v2.0/token"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=data) as resp:
            return await resp.json()


async def exchange_code(cfg: OAuthConfig, code: str) -> dict:
    """Exchange an authorization code for the user's tokens."""
    return await _token_request(
        cfg,
        {
            "grant_type": "authorization_code",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "code": code,
            "redirect_uri": redirect_uri(cfg),
            "scope": _scope(cfg),
        },
    )


async def refresh_token(cfg: OAuthConfig, refresh: str) -> dict:
    """Refresh the user's access token using a stored refresh token."""
    return await _token_request(
        cfg,
        {
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": refresh,
            "scope": _scope(cfg),
        },
    )
