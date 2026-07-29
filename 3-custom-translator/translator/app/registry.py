"""Per-agent registry for the shared translator router.

One shared Container App serves MANY published agents. Each incoming Bot
Framework activity carries the target bot's app id in ``recipient.id``; this
registry maps that bot id to the agent's runtime config: which Foundry agent to
run, which Bot Framework credentials to validate/sign with, and whether the
agent needs a per-user delegated token (``oauth``) or just the
container's app identity (``app``).

Backends (first non-empty wins):
  1. ``AGENT_REGISTRY_JSON``   — inline JSON blob (env var)
  2. ``AGENT_REGISTRY_FILE``   — path to a JSON file (mounted secret/volume)
  3. ``AGENT_REGISTRY_TABLE``  — Azure Table (partition ``agent``, row = bot app id)
  4. fallback                  — a single agent synthesized from the ``BOT_*`` /
                                 ``OAUTH_*`` env vars (backwards compatible with
                                 the Stage-1 single-agent deployment).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


def normalize_bot_id(raw: str) -> str:
    """Channel account ids may be prefixed (e.g. ``28:<appId>``); return the appId."""
    if not raw:
        return ""
    return raw.split(":")[-1].strip().lower()


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    tenant_id: str
    scope: str
    redirect_base_url: str


@dataclass
class AgentConfig:
    bot_app_id: str
    bot_app_password: str
    bot_app_tenant_id: str
    bot_app_type: str
    foundry_agent_name: str
    auth_mode: str  # "app" | "oauth"
    oauth: Optional[OAuthConfig] = None

    @property
    def key(self) -> str:
        return normalize_bot_id(self.bot_app_id)


class AgentRegistry:
    def __init__(self, agents: list[AgentConfig], loader=None) -> None:
        self._agents = [a for a in agents if a.bot_app_id]
        self._by_id = {a.key: a for a in self._agents}
        # Optional read-through loader: (normalized_bot_id) -> AgentConfig | None.
        # Present for the Table backend so agents published AFTER startup are
        # picked up on their first message (no container restart needed).
        self._loader = loader
        log.info(
            "agent registry loaded: %d agent(s) %s (read-through=%s)",
            len(self._agents),
            [a.foundry_agent_name for a in self._agents],
            self._loader is not None,
        )

    def get(self, bot_id: str) -> Optional[AgentConfig]:
        key = normalize_bot_id(bot_id)
        cfg = self._by_id.get(key)
        if cfg is None and self._loader is not None and key:
            cfg = self._loader(key)
            if cfg is not None:
                self._by_id[cfg.key] = cfg
                self._agents.append(cfg)
                log.info(
                    "agent registry: read-through loaded %s -> %s",
                    key,
                    cfg.foundry_agent_name,
                )
        return cfg

    def all(self) -> list[AgentConfig]:
        return list(self._agents)

    def resolve(self, bot_id: str) -> Optional[AgentConfig]:
        """Match by bot id (with read-through for the Table backend). For a
        static single-agent deployment return the sole entry (APIM has already
        validated the JWT audience upstream)."""
        cfg = self.get(bot_id)
        if cfg:
            return cfg
        if self._loader is None and len(self._agents) == 1:
            return self._agents[0]
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _oauth_from_dict(d: Optional[dict]) -> Optional[OAuthConfig]:
    if not d:
        return None
    return OAuthConfig(
        client_id=d.get("clientId", ""),
        client_secret=d.get("clientSecret", ""),
        tenant_id=d.get("tenantId", ""),
        scope=d.get("scope", ""),
        redirect_base_url=d.get("redirectBaseUrl", ""),
    )


def _agent_from_dict(d: dict) -> AgentConfig:
    auth_mode = (d.get("authMode") or "app").lower()
    return AgentConfig(
        bot_app_id=d["botAppId"],
        bot_app_password=d.get("botAppPassword", ""),
        bot_app_tenant_id=d.get("botAppTenantId", ""),
        bot_app_type=d.get("botAppType", "SingleTenant"),
        foundry_agent_name=d["foundryAgentName"],
        auth_mode=auth_mode,
        oauth=_oauth_from_dict(d.get("oauth")) if auth_mode == "oauth" else None,
    )


def _from_json_blob(blob: str) -> list[AgentConfig]:
    data = json.loads(blob)
    items = data.get("agents", []) if isinstance(data, dict) else data
    return [_agent_from_dict(x) for x in items]


def _entity_to_agent(e) -> AgentConfig:
    auth_mode = (e.get("authMode") or "app").lower()
    oauth = None
    if auth_mode == "oauth":
        oauth = OAuthConfig(
            client_id=e.get("oauthClientId", ""),
            client_secret=e.get("oauthClientSecret", ""),
            tenant_id=e.get("oauthTenantId", ""),
            scope=e.get("oauthScope", ""),
            redirect_base_url=e.get("oauthRedirectBaseUrl", ""),
        )
    return AgentConfig(
        bot_app_id=e.get("RowKey", ""),
        bot_app_password=e.get("botAppPassword", ""),
        bot_app_tenant_id=e.get("botAppTenantId", ""),
        bot_app_type=e.get("botAppType", "SingleTenant"),
        foundry_agent_name=e.get("foundryAgentName", ""),
        auth_mode=auth_mode,
        oauth=oauth,
    )


class _TableBackend:
    """Table Storage backend with an initial snapshot + per-row read-through.

    Uses the sync Table client. ``load_all`` runs once at startup; ``load_one``
    is called on a cache miss (a bot published after startup) and does a single
    ``get_entity`` — a brief blocking call, fine at this scale, and only on the
    first message for a never-seen bot.
    """

    PARTITION = "agent"

    def __init__(self, account_url: str, table_name: str) -> None:
        from azure.data.tables import TableClient
        from azure.identity import DefaultAzureCredential

        self._cred = DefaultAzureCredential()
        self._client = TableClient(
            endpoint=account_url, table_name=table_name, credential=self._cred
        )

    def load_all(self) -> list[AgentConfig]:
        agents: list[AgentConfig] = []
        try:
            for e in self._client.list_entities():
                agents.append(_entity_to_agent(e))
        except Exception:  # noqa: BLE001
            log.warning(
                "registry: initial table load skipped (table may not exist yet)"
            )
        return agents

    def load_one(self, row_key: str) -> Optional[AgentConfig]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            e = self._client.get_entity(partition_key=self.PARTITION, row_key=row_key)
            return _entity_to_agent(e)
        except ResourceNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            log.exception("registry: read-through lookup failed for %s", row_key)
            return None


def _single_from_env() -> list[AgentConfig]:
    """Backwards-compatible single agent from the BOT_* / OAUTH_* env vars."""
    if not settings.bot_app_id:
        log.warning("no registry configured and BOT_APP_ID unset — registry is empty")
        return []
    auth_mode = "oauth" if settings.mcp_oauth_enabled else "app"
    oauth = None
    if auth_mode == "oauth":
        oauth = OAuthConfig(
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            tenant_id=settings.oauth_tenant_id,
            scope=settings.oauth_mcp_scope,
            redirect_base_url=settings.oauth_redirect_base_url,
        )
    return [
        AgentConfig(
            bot_app_id=settings.bot_app_id,
            bot_app_password=settings.bot_app_password,
            bot_app_tenant_id=settings.bot_app_tenant_id,
            bot_app_type=settings.bot_app_type,
            foundry_agent_name=settings.foundry_agent_name,
            auth_mode=auth_mode,
            oauth=oauth,
        )
    ]


def build_registry() -> AgentRegistry:
    if settings.agent_registry_json:
        log.info("agent registry source: inline JSON")
        return AgentRegistry(_from_json_blob(settings.agent_registry_json))
    if settings.agent_registry_file:
        log.info("agent registry source: file %s", settings.agent_registry_file)
        with open(settings.agent_registry_file, "r", encoding="utf-8") as f:
            return AgentRegistry(_from_json_blob(f.read()))
    if settings.agent_registry_table_name:
        log.info(
            "agent registry source: table %s (read-through)",
            settings.agent_registry_table_name,
        )
        if not settings.thread_table_url:
            log.warning("AGENT_REGISTRY_TABLE set but THREAD_TABLE_URL missing")
            return AgentRegistry([])
        backend = _TableBackend(
            settings.thread_table_url, settings.agent_registry_table_name
        )
        return AgentRegistry(backend.load_all(), loader=backend.load_one)
    log.info("agent registry source: single-agent env fallback (Stage 1)")
    return AgentRegistry(_single_from_env())
