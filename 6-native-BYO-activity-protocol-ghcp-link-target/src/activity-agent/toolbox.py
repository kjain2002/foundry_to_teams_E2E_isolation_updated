# Copyright (c) Microsoft. All rights reserved.
"""Explicit, consent-aware client for the linked Foundry toolbox (any MCP tools).

The Copilot SDK can mount a toolbox as an MCP server, but it drives the MCP
handshake *inside the runtime*: when one of the toolbox's connections needs
interactive authorization (OAuth identity passthrough) the toolbox returns a
``CONSENT_REQUIRED`` error that never surfaces to our code, so the user would
never see the sign-in link. This module calls the toolbox endpoint ourselves
(initialize -> tools/list -> tools/call) so we CAN catch ``CONSENT_REQUIRED`` and
hand the consent URL back to the user as a sign-in card.

Nothing here assumes a particular tool: the model lists whatever the toolbox
exposes (``list_tools``) and calls any of them by name (``call_tool``). Toolboxes
with *tool search* enabled expose a search + invoke pair instead of the real
tools; the model just calls those like any other tool.

The toolbox also serves **skills** (SKILL.md documents). ``skills_system_text``
puts a one-line index in the system prompt and ``get_skill_markdown`` returns a
skill's full text only when the model asks for it.

The toolbox URL comes from the linked target agent (see ``target_agent`` /
``target-agent.yaml``). Auth uses the agent identity's
``https://ai.azure.com/.default`` bearer token.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

import target_agent

logger = logging.getLogger("github-copilot.toolbox")

_SCOPE = "https://ai.azure.com/.default"
_credential = DefaultAzureCredential()

# Resolve the toolbox from the linked target agent (first MCP server, if any).
# Import must never fail: a bad/missing target just disables the toolbox tools,
# it must not take the whole agent down (tools.py imports this at startup).
try:
    _TARGET = target_agent.load()
    _SERVER = _TARGET.mcp_servers[0] if _TARGET.mcp_servers else None
except Exception as _ex:  # pylint: disable=broad-exception-caught
    logger.warning("Could not load target toolbox (%s); toolbox tools disabled.", _ex)
    _SERVER = None


def is_available() -> bool:
    """True when the linked target agent carries a toolbox/MCP server."""
    return _SERVER is not None


def server_label() -> str:
    return (_SERVER or {}).get("label", "toolbox")


def _token() -> str:
    return _credential.get_token(_SCOPE).token


def _parse(body: str) -> dict | None:
    body = (body or "").strip()
    if not body:
        return None
    if body.startswith("{"):
        try:
            return json.loads(body)
        except ValueError:
            return None
    # SSE framing: use the last 'data:' line.
    for line in reversed(body.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[len("data:"):].strip())
            except ValueError:
                return None
    return None


_CONSENT_RE = re.compile(r'https:(?://|\\/\\/)[^"\s]+')


def _normalize_url(url: str) -> str:
    cleaned = (url or "").replace("\\/", "/")
    return cleaned.rstrip("'\"],}")


def _error_messages(raw: str) -> list[str]:
    msgs: list[str] = []
    parsed = _parse(raw) or {}
    err = parsed.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            msgs.append(msg)
        data = err.get("data")
        if isinstance(data, str) and data:
            msgs.append(data)
        elif isinstance(data, dict):
            try:
                msgs.append(json.dumps(data))
            except Exception:  # pylint: disable=broad-exception-caught
                pass
    if raw:
        msgs.append(raw)
    return msgs


def _consent_url(raw: str) -> str | None:
    """Pull the consent URL out of a CONSENT_REQUIRED error payload.

    The toolbox embeds the error as backslash-escaped JSON inside the JSON-RPC
    error message, so match the first URL after the CONSENT_REQUIRED marker and
    stop at a quote/backslash/whitespace (the url itself has none of those).
    """
    if not raw:
        return None
    for msg in _error_messages(raw):
        if "CONSENT_REQUIRED" not in msg:
            continue
        m = _CONSENT_RE.search(msg, msg.find("CONSENT_REQUIRED"))
        if not m:
            continue
        url = _normalize_url(m.group(0))
        logger.info("TOOLBOX consent_url_extracted=%s", url[:180])
        return url
    return None


def _headers(call_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # Per-user identity passthrough: the toolbox proxy resolves the END USER from
    # this opaque per-request id and applies their OAuth consent. Forward it
    # verbatim; without it the proxy can't tell who is calling and keeps asking
    # the user to sign in.
    if call_id:
        headers["x-agent-foundry-call-id"] = call_id
    return headers


def _open(c: httpx.Client, url: str) -> tuple[dict[str, str], str | None]:
    """MCP initialize; returns (session headers, consent url if one was demanded)."""
    r = c.post(url, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "activity-agent", "version": "1.0"}},
    })
    consent = _consent_url(r.text)
    if consent:
        return {}, consent
    r.raise_for_status()
    sid = r.headers.get("mcp-session-id")
    return ({"mcp-session-id": sid} if sid else {}), None


def _mcp(c: httpx.Client, url: str, h2: dict, method: str, params: dict) -> tuple[dict, str]:
    r = c.post(url, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}, headers=h2)
    return _parse(r.text) or {}, r.text


def _summarize_tool(t: dict) -> dict[str, Any]:
    """Name, description and input schema: what the model needs to call a tool."""
    return {
        "name": t.get("name"),
        "description": (t.get("description") or "")[:500],
        "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
    }


def list_tools(call_id: str | None = None) -> dict[str, Any]:
    """List the toolbox's tools.

    Returns one of:
      {"status": "consent", "url": <consent-url>}  -> user must authorize first
      {"status": "ok", "tools": [{name, description, input_schema}, ...]}
      {"status": "error", "message": <detail>}
    """
    if _SERVER is None:
        return {"status": "error", "message": "No toolbox is linked to this agent."}
    url = _SERVER["url"]
    try:
        with httpx.Client(timeout=90, headers=_headers(call_id)) as c:
            h2, consent = _open(c, url)
            if consent:
                return {"status": "consent", "url": consent}
            parsed, raw = _mcp(c, url, h2, "tools/list", {})
            consent = _consent_url(raw)
            if consent:
                return {"status": "consent", "url": consent}
            if "error" in parsed:
                return {"status": "error", "message": json.dumps(parsed["error"])[:500]}
            tools = [t for t in (parsed.get("result") or {}).get("tools") or [] if isinstance(t, dict)]
            logger.info("TOOLBOX_LIST call_id_present=%s tools=%s",
                        bool(call_id), [t.get("name") for t in tools])
            return {"status": "ok", "tools": [_summarize_tool(t) for t in tools]}
    except httpx.HTTPStatusError as ex:
        return {"status": "error", "message": f"HTTP {ex.response.status_code}: {ex.response.text[:400]}"}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.error("toolbox list_tools failed: %s", ex, exc_info=True)
        return {"status": "error", "message": f"{type(ex).__name__}: {ex}"}


def call_tool(name: str, arguments: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    """Call one toolbox tool by name.

    Returns one of:
      {"status": "consent", "url": <consent-url>}  -> user must authorize first
      {"status": "ok", "text": <result-text>}
      {"status": "error", "message": <detail>}
    """
    if _SERVER is None:
        return {"status": "error", "message": "No toolbox is linked to this agent."}
    url = _SERVER["url"]
    try:
        with httpx.Client(timeout=120, headers=_headers(call_id)) as c:
            h2, consent = _open(c, url)
            if consent:
                return {"status": "consent", "url": consent}
            parsed, raw = _mcp(c, url, h2, "tools/call", {"name": name, "arguments": arguments or {}})
            consent = _consent_url(raw)
            if consent:
                return {"status": "consent", "url": consent}
            logger.info("TOOLBOX_CALL tool=%s call_id_present=%s response=%s",
                        name, bool(call_id), raw[:300])
            if "error" in parsed:
                return {"status": "error", "message": f"[tool={name}] " + json.dumps(parsed["error"])[:500]}
            result = parsed.get("result") or {}
            texts = [b.get("text", "") for b in (result.get("content") or []) if isinstance(b, dict)]
            joined = "\n".join(t for t in texts if t)
            if result.get("isError"):
                return {"status": "error", "message": f"[tool={name}] " + (joined or json.dumps(result)[:500])}
            return {"status": "ok", "text": joined or json.dumps(result)[:4000]}
    except httpx.HTTPStatusError as ex:
        return {"status": "error", "message": f"HTTP {ex.response.status_code}: {ex.response.text[:400]}"}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.error("toolbox call_tool failed: %s", ex, exc_info=True)
        return {"status": "error", "message": f"{type(ex).__name__}: {ex}"}


# ── Toolbox skills (io.modelcontextprotocol/skills) ──────────────────────────
# Skills are toolbox content, NOT gated by a per-user connection, so the agent
# identity's own token reads them. The toolbox only lists skills to a caller with
# a data-plane role on the project: grant the agent identity **Foundry User** on
# the project, or skills/list silently returns an empty list (see README).
_SKILLS_CACHE: list[dict] | None = None


def fetch_skills() -> list[dict]:
    """List toolbox skills and read each SKILL.md. Cached once non-empty.

    Each entry: {name, description, uri, markdown}.
    """
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None:
        return _SKILLS_CACHE
    out: list[dict] = []
    if _SERVER is not None:
        url = _SERVER["url"]
        try:
            with httpx.Client(timeout=60, headers=_headers()) as c:
                h2, _ = _open(c, url)
                listed, _ = _mcp(c, url, h2, "skills/list", {})
                raw_skills = (listed.get("result") or {}).get("skills") or []
                logger.info("SKILLS_LIST count=%d error=%s", len(raw_skills),
                            json.dumps(listed.get("error"))[:500] if listed.get("error") else None)
                for s in raw_skills:
                    if not isinstance(s, dict):
                        continue
                    fm = s.get("frontmatter") or {}
                    uri = s.get("uri")
                    md = ""
                    if uri:
                        rd, _ = _mcp(c, url, h2, "resources/read", {"uri": uri})
                        md = "\n".join(
                            b.get("text", "")
                            for b in (rd.get("result") or {}).get("contents") or []
                            if isinstance(b, dict)
                        )
                        logger.info("SKILL_READ uri=%s chars=%d error=%s", uri, len(md),
                                    json.dumps(rd.get("error"))[:300] if rd.get("error") else None)
                    out.append({"name": fm.get("name") or uri, "description": fm.get("description", ""),
                                "uri": uri, "markdown": md})
        except Exception as ex:  # pylint: disable=broad-exception-caught
            logger.warning("fetch_skills failed: %s", ex)
    # Cache only a successful fetch; an empty/failed one retries on the next call
    # instead of hiding the toolbox's skills for the whole session.
    if any(s.get("markdown") for s in out):
        _SKILLS_CACHE = out
    return out


def skills_system_text() -> str:
    """System-prompt index of toolbox skills: name + when to use, not the body.

    Skills load on demand like a tool: the model calls ``get_skill`` only when a
    request matches a skill's description, so unrelated turns don't carry every
    SKILL.md in the prompt.
    """
    skills = [s for s in fetch_skills() if s.get("markdown")]
    if not skills:
        return ""
    parts = ["--- Toolbox skills (load on demand) ---",
             "Before doing a task that matches one of these skills, call the "
             "get_skill tool with its name and follow the instructions it returns. "
             "Don't load a skill for unrelated requests."]
    for s in skills:
        desc = " ".join((s.get("description") or "").split())
        parts.append(f"- {s['name']}: {desc}")
    return "\n".join(parts)


def get_skill_markdown(name: str | None = None) -> str:
    """Return a skill's raw SKILL.md (by partial name), or the skill listing.

    An empty or unmatched ``name`` returns only names + descriptions, so the
    model can pick a skill without loading every body.
    """
    skills = fetch_skills()
    if not skills:
        return "No skills are available from the toolbox."
    if name:
        for s in skills:
            if name.lower() in (s.get("name") or "").lower():
                return s.get("markdown") or f"(skill '{s['name']}' has no content)"
    listing = "; ".join(f"{s.get('name')}: {' '.join((s.get('description') or '').split())[:200]}"
                        for s in skills)
    return f"Available toolbox skills — {listing}. Call get_skill again with one name."
