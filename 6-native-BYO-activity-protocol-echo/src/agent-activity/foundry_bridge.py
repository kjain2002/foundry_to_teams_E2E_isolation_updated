# Copyright (c) Microsoft. All rights reserved.

"""Thin bridge to the EXISTING deployed agent's Responses endpoint.

Mirrors the proven mechanism in the translator's ``foundry.py``: authenticate to
Foundry with the container's managed identity (AIProjectClient), and forward the
signed-in user's token as the ``x-ms-user-token`` header on ``responses.create``.
Foundry then OBO-exchanges that user token so the toolbox (Starburst MCP) runs as
the user. The agent's brain/tool loop runs inside the existing agent; we do not
rebuild it here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from config import FOUNDRY_PROJECT_ENDPOINT, TOOLBOX_MCP_ENDPOINT

logger = logging.getLogger("agent-activity.bridge")

_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
# Logic Apps managed-connector consent URL the toolbox returns on
# CONSENT_REQUIRED (-32006), wherever it appears in the error body.
_CONSENT_URL_RE = re.compile(r"https://[^\s\"\\]+consent\.azure-apihub\.net/[^\s\"\\]+")


@dataclass
class BridgeReply:
    text: str | None = None
    consent_link: str | None = None


class FoundryBridge:
    def __init__(self) -> None:
        self._cred = DefaultAzureCredential()
        self._project = AIProjectClient(
            endpoint=FOUNDRY_PROJECT_ENDPOINT,
            credential=self._cred,
            allow_preview=True,
        )
        self._openai_by_agent: dict = {}

    def _openai_for(self, agent_name: str):
        client = self._openai_by_agent.get(agent_name)
        if client is None:
            client = self._project.get_openai_client(agent_name=agent_name)
            self._openai_by_agent[agent_name] = client
        return client

    @property
    def credential(self):
        """The managed-identity credential (used for container REST calls)."""
        return self._cred

    async def chat(
        self,
        agent_name: str,
        user_text: str,
        user_token: str | None = None,
        history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> "BridgeReply":
        """Send one user turn; return the reply text, or a consent_link.

        Conversation state is carried as a full ``history`` array with
        ``store=False``. The user token, when present, is forwarded as
        ``x-ms-user-token`` so the toolbox runs under the user's identity. A
        ``session_id`` is passed as ``agent_session_id`` so the agent's $HOME
        (session sandbox) holds the uploaded/generated files.
        """
        openai = self._openai_for(agent_name)
        input_messages = (history or []) + [{"role": "user", "content": user_text}]

        kwargs: dict = {"input": input_messages, "store": False}
        if user_token:
            kwargs["extra_headers"] = {"x-ms-user-token": f"Bearer {user_token}"}
        if session_id:
            kwargs["extra_body"] = {"agent_session_id": session_id}

        resp = await openai.responses.create(**kwargs)

        # Active consent handling: detect CONSENT_REQUIRED and surface a clean
        # link, rather than relying on the platform to render a card (which is
        # unreliable when a token has expired).
        consent = _find_consent_url(resp)
        text = _extract_text(resp)
        if consent is None and _looks_like_consent_prompt(text):
            consent = await self.probe_consent()
        if consent:
            return BridgeReply(consent_link=consent)
        return BridgeReply(text=text)

    async def probe_consent(self) -> str | None:
        """Probe the toolbox (tools/list) and extract the consent URL, if any."""
        if not TOOLBOX_MCP_ENDPOINT:
            return None
        try:
            token = (await self._cred.get_token(_FOUNDRY_SCOPE)).token
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    TOOLBOX_MCP_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                body = r.text
            m = _CONSENT_URL_RE.search(body)
            return m.group(0) if m else None
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("consent probe failed", exc_info=True)
            return None

    async def close(self) -> None:
        for client in self._openai_by_agent.values():
            try:
                await client.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        await self._project.close()
        await self._cred.close()


def _extract_text(resp) -> str:
    """Pull the assistant text out of a Responses result."""
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    for item in getattr(resp, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            t = getattr(content, "text", None)
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts) or "(no response)"


def _find_consent_url(resp) -> str | None:
    """Search the serialized response for a consent.azure-apihub.net link."""
    try:
        dump = getattr(resp, "model_dump_json", None)
        blob = dump() if callable(dump) else str(resp)
    except Exception:  # pylint: disable=broad-exception-caught
        blob = str(resp)
    m = _CONSENT_URL_RE.search(blob)
    return m.group(0) if m else None


def _looks_like_consent_prompt(text: str | None) -> bool:
    """Heuristic: the deployed agent replied asking the user to consent."""
    if not text:
        return False
    t = text.lower()
    return "consent" in t or ("sign in" in t and "resume" in t)
