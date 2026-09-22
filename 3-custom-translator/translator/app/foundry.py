"""Foundry Prompt Agent client wrapper.

Uses the OpenAI Responses API exposed by the Foundry Agent endpoint:
  {project_endpoint}/agents/{agent_name}/endpoint/protocols/openai

Conversation continuity uses the Responses API's `previous_response_id`
chain, persisted via the ThreadStore (now storing response_id per
conversation_id).

Authentication: DefaultAzureCredential (UAMI in Container Apps,
AZURE_CLIENT_ID env var pins it to the translator MI).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from .config import settings
from .state import ThreadStore

log = logging.getLogger(__name__)

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
# Match the Logic Apps managed-connector consent URL wherever it appears in the
# toolbox error body (robust to JSON escaping).
_CONSENT_URL_RE = re.compile(r"https://[^\s\"\\]+consent\.azure-apihub\.net/[^\s\"\\]+")


async def _probe_toolbox_consent(endpoint: str, credential) -> "Optional[str]":
    """Return the toolbox reconnect URL if it reports CONSENT_REQUIRED, else None."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        token = await credential.get_token(FOUNDRY_SCOPE)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as r:
                body = await r.text()
        m = _CONSENT_URL_RE.search(body)
        return m.group(0) if m else None
    except Exception:  # noqa: BLE001
        log.warning("toolbox consent probe failed", exc_info=True)
        return None


@dataclass
class GeneratedFile:
    """A file the agent's Code Interpreter produced inside a container."""

    container_id: Optional[str]
    file_id: str
    filename: str


@dataclass
class ChatReply:
    """Result of one agent turn. Either ``text`` (answer) or ``consent_link``
    (Foundry returned an oauth_consent_request the user must authorize).

    ``generated_files`` lists any Code Interpreter output files cited in the
    response (via container_file_citation annotations) so the caller can
    deliver them back to the user."""

    text: Optional[str]
    consent_link: Optional[str] = None
    generated_files: list["GeneratedFile"] = field(default_factory=list)


class FoundryClient:
    def __init__(self, store: ThreadStore) -> None:
        self._cred = DefaultAzureCredential()
        self._project = AIProjectClient(
            endpoint=settings.foundry_project_endpoint,
            credential=self._cred,
            allow_preview=True,
        )
        # One shared project client; one OpenAI client cached per agent name so a
        # single container can route turns to many published agents.
        self._openai_by_agent: dict = {}
        self._store = store

    def _openai_for(self, agent_name: str):
        client = self._openai_by_agent.get(agent_name)
        if client is None:
            client = self._project.get_openai_client(agent_name=agent_name)
            self._openai_by_agent[agent_name] = client
        return client

    async def close(self) -> None:
        for client in self._openai_by_agent.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        await self._project.close()
        await self._cred.close()

    @property
    def credential(self):
        """The managed-identity credential (used for container REST calls)."""
        return self._cred

    async def chat(
        self,
        agent_name: str,
        conversation_id: str,
        user_text: str,
        user_token: str | None = None,
        container_id: str | None = None,
        history: list[dict] | None = None,
        attachments_manifest: list[dict] | None = None,
    ) -> "ChatReply":
        """Send one user turn to ``agent_name`` using the proven UI contract.

        Mirrors the working reference client: conversation state is carried as a
        full ``history`` array in ``input`` with ``store=False`` (this hosted
        agent does not persist responses, so ``previous_response_id`` cannot be
        used). The client-created ``container_id`` is passed as a
        ``USE_CONTAINER_ID=<id>`` line in the latest user message so the agent
        binds Code Interpreter to it; uploaded attachments are advertised via a
        manifest. Generated files are detected by the caller by diffing the
        container's file list (the agent does not emit container_file_citation).
        """
        openai = self._openai_for(agent_name)

        hint_lines: list[str] = []
        if container_id:
            hint_lines.append(f"USE_CONTAINER_ID={container_id}")
        if attachments_manifest:
            manifest = "\n".join(
                f"- {a['filename']}: {a.get('path') or a['filename']}"
                for a in attachments_manifest
            )
            hint_lines.append(
                "User attachments are available in the Code Interpreter "
                "container. Use code_interpreter to inspect or use them when "
                "relevant:\n" + manifest
            )
        content = (
            "\n".join(hint_lines) + "\n\n" + user_text if hint_lines else user_text
        )
        input_messages = (history or []) + [{"role": "user", "content": content}]

        kwargs: dict = {"input": input_messages, "store": False}
        if user_token:
            kwargs["extra_headers"] = {"x-ms-user-token": f"Bearer {user_token}"}

        def _err_msg(r) -> str:
            e = getattr(r, "error", None)
            return (
                getattr(e, "message", None)
                or (e.get("message") if isinstance(e, dict) else None)
                or ""
            )

        try:
            resp = await openai.responses.create(**kwargs)
        except Exception:
            log.exception("responses.create failed (conv=%s)", conversation_id)
            raise

        if _err_msg(resp) == "create_session":
            # Session init failed. The common cause is the toolbox connection
            # needing re-authorization (CONSENT_REQUIRED). Probe the toolbox: if
            # consent is needed, surface the reconnect link to the user now;
            # otherwise treat it as a transient cold start and retry once.
            consent_url = None
            if settings.toolbox_mcp_endpoint:
                consent_url = await _probe_toolbox_consent(
                    settings.toolbox_mcp_endpoint, self._cred
                )
            if consent_url:
                log.warning(
                    "toolbox consent required (conv=%s) — surfacing reconnect link",
                    conversation_id,
                )
                return ChatReply(text=None, consent_link=consent_url)
            log.warning(
                "create_session (transient?) conv=%s — retrying once after 5s",
                conversation_id,
            )
            await asyncio.sleep(5)
            try:
                resp = await openai.responses.create(**kwargs)
            except Exception:
                log.exception("responses.create retry failed (conv=%s)", conversation_id)
                raise

        consent_link = _extract_consent_link(resp)
        text = _extract_text(resp)

        # DIAG (temporary): log the output structure so we can see whether the
        # agent actually invoked Code Interpreter this turn.
        try:
            diag = []
            for it in (getattr(resp, "output", None) or []):
                ittype = getattr(it, "type", None) or (
                    it.get("type") if isinstance(it, dict) else None
                )
                diag.append(ittype)
            log.info("DIAG output_types=%s", diag)
        except Exception:  # noqa: BLE001
            log.exception("DIAG failed")

        return ChatReply(text=text, consent_link=consent_link)


def _extract_consent_link(resp) -> Optional[str]:
    """Return the consent_link if Foundry emitted an oauth_consent_request."""
    out = getattr(resp, "output", None) or []
    for item in out:
        itype = getattr(item, "type", None)
        if itype is None and isinstance(item, dict):
            itype = item.get("type")
        if itype == "oauth_consent_request":
            link = getattr(item, "consent_link", None)
            if link is None and isinstance(item, dict):
                link = item.get("consent_link")
            if link:
                return link
    return None


def _extract_text(resp) -> Optional[str]:
    # Preferred: SDK exposes a flat output_text helper.
    t = getattr(resp, "output_text", None)
    if t:
        return t
    # Fallback: walk response.output[].content[].text
    out = getattr(resp, "output", None) or []
    chunks: list[str] = []
    for item in out:
        content = getattr(item, "content", None) or []
        for c in content:
            v = getattr(c, "text", None)
            if isinstance(v, str) and v:
                chunks.append(v)
            elif v is not None:
                inner = getattr(v, "value", None)
                if inner:
                    chunks.append(inner)
    return "\n".join(chunks) if chunks else None


def _ann_get(ann, key):
    val = getattr(ann, key, None)
    if val is None and isinstance(ann, dict):
        val = ann.get(key)
    return val


def _extract_generated_files(resp) -> list["GeneratedFile"]:
    """Return files cited via container_file_citation annotations in the output."""
    files: list[GeneratedFile] = []
    seen: set = set()
    out = getattr(resp, "output", None) or []
    for item in out:
        content = getattr(item, "content", None) or []
        for c in content:
            anns = getattr(c, "annotations", None) or []
            for ann in anns:
                atype = _ann_get(ann, "type")
                if atype != "container_file_citation":
                    continue
                fid = _ann_get(ann, "file_id")
                cid = _ann_get(ann, "container_id")
                fn = _ann_get(ann, "filename") or "file"
                if fid and (cid, fid) not in seen:
                    seen.add((cid, fid))
                    files.append(
                        GeneratedFile(container_id=cid, file_id=fid, filename=fn)
                    )
    return files

