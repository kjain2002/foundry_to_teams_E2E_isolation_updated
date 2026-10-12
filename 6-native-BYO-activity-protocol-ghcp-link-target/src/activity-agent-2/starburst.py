# Copyright (c) Microsoft. All rights reserved.
"""Explicit, consent-aware client for a Foundry toolbox (Starburst) MCP endpoint.

The Copilot SDK can mount a toolbox as an MCP server, but it drives the MCP
handshake *inside the runtime*: when the toolbox's connection needs interactive
authorization it returns a ``CONSENT_REQUIRED`` error that never surfaces to our
code, so we can never show the user the sign-in link. This module calls the
toolbox endpoint ourselves (initialize -> tools/list -> tools/call) so we CAN
catch ``CONSENT_REQUIRED`` and hand the consent URL back to the user as a card.

The toolbox URL + project connection come from the linked target agent (see
``target_agent`` / ``target-agent.yaml``). Auth uses the same
``https://ai.azure.com/.default`` bearer the rest of the agent mints.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

import target_agent

logger = logging.getLogger("github-copilot.starburst")

_SCOPE = "https://ai.azure.com/.default"
_credential = DefaultAzureCredential()

# Resolve the toolbox from the linked target agent (first MCP server, if any).
# Import must never fail: a bad/missing target just disables the Starburst tool,
# it must not take the whole agent down (tools.py imports this at startup).
try:
    _TARGET = target_agent.load()
    _SERVER = _TARGET.mcp_servers[0] if _TARGET.mcp_servers else None
except Exception as _ex:  # pylint: disable=broad-exception-caught
    logger.warning("Could not load target toolbox (%s); Starburst tool disabled.", _ex)
    _SERVER = None


def is_available() -> bool:
    """True when the linked target agent carries a toolbox/MCP server to query."""
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


_CONSENT_RE = re.compile(r'https:(?://|\\/\\/)[^"\s]+' )


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
    """Pull the APIM consent URL out of a CONSENT_REQUIRED error payload.

    The toolbox embeds the error as backslash-escaped JSON inside the JSON-RPC
    error message, so match the first URL after the CONSENT_REQUIRED marker and
    stop at a quote/backslash/whitespace (the url itself has none of those).
    """
    if not raw:
        return None
    for msg in _error_messages(raw):
        if "CONSENT_REQUIRED" not in msg and "consent.azure-apim.net/login" not in msg:
            continue
        idx = msg.find("CONSENT_REQUIRED")
        if idx < 0:
            idx = 0
        m = _CONSENT_RE.search(msg, idx)
        if not m:
            continue
        url = _normalize_url(m.group(0))
        logger.info("SBDIAG consent_url_extracted=%s", url[:180])
        return url
    return None


# Argument key names a SQL/execute tool might use for the statement.
_SQL_KEYS = ("sqlquery", "sql_query", "sql", "query", "statement", "command")


def _build_sql_args(schema: dict | None, sql: str) -> dict[str, Any] | None:
    """Place ``sql`` into the right argument field per the tool's JSON input schema.

    Returns the ``arguments`` object, or None when the schema has no recognizable
    SQL field (so a name-based fallback can decide).
    """
    props = (schema or {}).get("properties") or {}
    for p in props:
        if p.lower() in _SQL_KEYS:
            return {p: sql}
    body = props.get("body")
    if isinstance(body, dict):
        bprops = body.get("properties") or {}
        for p in bprops:
            if p.lower() in _SQL_KEYS:
                return {"body": {p: sql}}
        return {"body": {"sqlQuery": sql}}  # body wrapper but unknown key
    return None


def _inventory(tools: list[dict]) -> str:
    """Human-readable 'name(param,param)' list of every tool for diagnostics."""
    parts: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        schema = t.get("inputSchema") or t.get("input_schema") or {}
        props = list((schema.get("properties") or {}).keys())
        body = (schema.get("properties") or {}).get("body")
        if isinstance(body, dict):
            bp = list((body.get("properties") or {}).keys())
            props = [f"body.{k}" for k in bp] or props
        parts.append(f"{t.get('name')}({','.join(props)})")
    return "; ".join(parts)


# Meta/discovery tools that must never be used to run SQL.
_META_TOOLS = ("tool_search", "search", "list_tool", "describe", "get_schema", "list_tables")


def _select_sql_tool(tools: list[dict], sql: str) -> tuple[dict, dict[str, Any]] | None:
    """Choose the SQL/execute tool and build its arguments. Returns (tool, args)."""
    scored: list[tuple[int, dict, dict[str, Any]]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = (t.get("name") or "").lower()
        if any(m in name for m in _META_TOOLS):
            continue  # never run SQL through a discovery/meta tool
        name_sql = any(k in name for k in ("execut", "quer", "sql", "statement"))
        schema = t.get("inputSchema") or t.get("input_schema") or t.get("parameters")
        args = _build_sql_args(schema, sql)
        if args is not None:
            scored.append((3 if name_sql else 2, t, args))
        elif name_sql:
            scored.append((1, t, {"body": {"sqlQuery": sql}}))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1], scored[0][2]


# Facade toolboxes don't list real tools: they expose a search + invoke pair.
_FACADE_NAMES = {"tool_search", "call_tool", "search", "invoke_tool", "list_tools"}


def _tools_from_search(result: dict) -> list[dict]:
    """Extract tool definitions returned by a tool_search call."""
    out: list[dict] = []
    for key in ("tools", "results", "items"):
        v = result.get(key)
        if isinstance(v, list):
            out.extend(t for t in v if isinstance(t, dict))
    for blk in result.get("content") or []:
        if not (isinstance(blk, dict) and blk.get("type") == "text"):
            continue
        try:
            data = json.loads(blk.get("text", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(data, list):
            out.extend(t for t in data if isinstance(t, dict))
        elif isinstance(data, dict):
            for key in ("tools", "results", "items"):
                if isinstance(data.get(key), list):
                    out.extend(t for t in data[key] if isinstance(t, dict))
    return out


def _run_via_facade(c, url: str, h2: dict, sql: str, cid: str, names: list) -> dict[str, Any]:
    """tool_search -> pick SQL tool -> call_tool for facade-style toolboxes."""
    search_name = next((n for n in names if "search" in (n or "").lower()), "tool_search")
    call_name = next(
        (n for n in names if (n or "").lower() in ("call_tool", "calltool", "invoke_tool", "invoke")),
        "call_tool",
    )
    search = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": search_name, "arguments": {"query": "execute SQL query statement", "limit": 25}},
    }
    rs = c.post(url, json=search, headers=h2)
    if _consent_url(rs.text):
        return {"status": "consent", "url": _consent_url(rs.text)}
    logger.info("SBDIAG search_response=%s", rs.text[:900])
    sres = (_parse(rs.text) or {}).get("result") or {}
    discovered = _tools_from_search(sres)
    dnames = [d.get("name") for d in discovered]
    logger.info("SBDIAG discovered=%s", dnames)
    if not discovered:
        return {"status": "error",
                "message": f"[call_id={cid}] tool_search returned nothing parseable. Raw: {rs.text[:400]}"}
    sel = _select_sql_tool(discovered, sql)
    if sel is None:
        return {"status": "error",
                "message": f"[call_id={cid}] tool_search found {dnames} but none runs SQL."}
    tool, inner_args = sel
    realname = tool.get("name")
    logger.info("SBDIAG facade call=%s real=%s args=%s", call_name, realname, json.dumps(inner_args)[:200])
    call = {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": call_name, "arguments": {"name": realname, "arguments": inner_args}},
    }
    rc = c.post(url, json=call, headers=h2)
    if _consent_url(rc.text):
        return {"status": "consent", "url": _consent_url(rc.text)}
    logger.info("SBDIAG facade_call_response=%s", rc.text[:700])
    parsed = _parse(rc.text) or {}
    if "error" in parsed:
        return {"status": "error",
                "message": f"[call_id={cid}, via={call_name}->{realname}, sent={json.dumps(inner_args)[:120]}] "
                           + json.dumps(parsed["error"])[:300]}
    result = parsed.get("result") or {}
    texts = [b.get("text", "") for b in (result.get("content") or []) if isinstance(b, dict)]
    joined = "\n".join(t for t in texts if t)
    if result.get("isError"):
        return {"status": "error",
                "message": f"[via={realname}, sent={json.dumps(inner_args)[:120]}] "
                           + (joined or json.dumps(result)[:400])}
    return {"status": "ok", "text": joined or json.dumps(result)[:1500]}


# ── Toolbox skills (io.modelcontextprotocol/skills) ──────────────────────────
# The toolbox can publish SKILL.md documents (e.g. pptx-generation). These are
# toolbox content, NOT gated by the per-user Starburst connection, so the agent's
# own token reads them. We list them and read each SKILL.md to make the skill
# available to the model.
_SKILLS_CACHE: list[dict] | None = None


def _mcp(c, url: str, h2: dict, method: str, params: dict) -> dict:
    r = c.post(url, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params}, headers=h2)
    return _parse(r.text) or {}


def fetch_skills() -> list[dict]:
    """List toolbox skills and read each SKILL.md. Cached; returns [] on failure.

    Each entry: {name, description, uri, markdown}.
    """
    global _SKILLS_CACHE
    if _SKILLS_CACHE is not None:
        return _SKILLS_CACHE
    out: list[dict] = []
    if _SERVER is not None:
        url = _SERVER["url"]
        headers = {
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        try:
            with httpx.Client(timeout=60, headers=headers) as c:
                ri = c.post(url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "activity-agent", "version": "1.0"}},
                })
                ri.raise_for_status()
                sid = ri.headers.get("mcp-session-id")
                h2 = {"mcp-session-id": sid} if sid else {}
                listed = _mcp(c, url, h2, "skills/list", {})
                raw_skills = (listed.get("result") or {}).get("skills") or []
                logger.info(
                    "SKILLS_LIST count=%d error=%s keys=%s",
                    len(raw_skills),
                    json.dumps(listed.get("error"))[:500] if listed.get("error") else None,
                    sorted(listed.keys()),
                )
                for s in raw_skills:
                    if not isinstance(s, dict):
                        continue
                    fm = s.get("frontmatter") or {}
                    uri = s.get("uri")
                    md = ""
                    if uri:
                        rd = _mcp(c, url, h2, "resources/read", {"uri": uri})
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


def _strip_frontmatter(md: str) -> str:
    if md.startswith("---"):
        end = md.find("---", 3)
        if end != -1:
            return md[end + 3:].strip()
    return md.strip()


def skills_system_text() -> str:
    """System-prompt index of toolbox skills: name + when to use, not the body.

    Skills load on demand like a tool: the model calls ``get_skill`` only when a
    request matches a skill's description (e.g. generating a .pptx), so unrelated
    turns don't carry every SKILL.md in the prompt.
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
    """Return a skill's raw SKILL.md (by partial name), or a listing."""
    skills = fetch_skills()
    if not skills:
        return "No skills are available from the toolbox."
    if name:
        for s in skills:
            if name.lower() in (s.get("name") or "").lower():
                return s.get("markdown") or f"(skill '{s['name']}' has no content)"
    if len(skills) == 1:
        return skills[0].get("markdown") or ""
    names = ", ".join(s.get("name") or "?" for s in skills)
    return f"Available toolbox skills: {names}. Ask for one by name."


def run_query(sql: str, call_id: str | None = None) -> dict[str, Any]:
    """Run ``sql`` against the toolbox.

    ``call_id`` is the hosted runtime's per-request Foundry call id
    (``x-agent-foundry-call-id``); forwarding it lets the toolbox proxy resolve
    the END USER and apply their per-user OAuth identity passthrough/consent.
    Without it the proxy can't tell who is calling and keeps asking to sign in.

    Returns one of:
      {"status": "consent", "url": <consent-url>}   -> user must authorize first
      {"status": "ok", "text": <result-text>}        -> query ran
      {"status": "error", "message": <detail>}       -> anything else
    """
    if _SERVER is None:
        return {"status": "error", "message": "No toolbox is linked to this agent."}
    url = _SERVER["url"]
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # Per-user identity passthrough: 1P toolbox proxy resolves the caller from
    # this opaque per-request id. Forward verbatim; never parse it.
    if call_id:
        headers["x-agent-foundry-call-id"] = call_id
    try:
        with httpx.Client(timeout=90, headers=headers) as c:
            init = {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "activity-agent", "version": "1.0"},
                },
            }
            r = c.post(url, json=init)
            if _consent_url(r.text):
                return {"status": "consent", "url": _consent_url(r.text)}
            r.raise_for_status()
            sid = r.headers.get("mcp-session-id")
            h2 = {"mcp-session-id": sid} if sid else {}

            # List tools; a connection needing auth reports CONSENT_REQUIRED here.
            rl = c.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h2)
            consent = _consent_url(rl.text)
            if consent:
                return {"status": "consent", "url": consent}
            listed = _parse(rl.text) or {}
            tools = (listed.get("result") or {}).get("tools") or []
            names = [t.get("name") for t in tools if isinstance(t, dict)]
            cid = "yes" if call_id else "NO"
            logger.info("SBDIAG call_id=%s tools=%s", cid, names)
            if not tools:
                return {"status": "error",
                        "message": f"[call_id={cid}] Toolbox returned no tools. {rl.text[:300]}"}

            # Facade toolbox: real tools are reached via tool_search + call_tool.
            lower = {(n or "").lower() for n in names}
            if lower and lower <= _FACADE_NAMES:
                return _run_via_facade(c, url, h2, sql, cid, names)

            sel = _select_sql_tool(tools, sql)
            if sel is None:
                return {"status": "error",
                        "message": f"[call_id={cid}] No runnable SQL tool. Toolbox exposes: {_inventory(tools)}"}
            tool, arguments = sel
            name = tool.get("name")
            logger.info("SBDIAG selected=%s args=%s schema=%s", name,
                        json.dumps(arguments)[:200],
                        json.dumps(tool.get("inputSchema") or {})[:400])
            call = {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
            rc = c.post(url, json=call, headers=h2)
            consent = _consent_url(rc.text)
            if consent:
                return {"status": "consent", "url": consent}
            logger.info("SBDIAG call_response=%s", rc.text[:600])
            parsed = _parse(rc.text) or {}
            if "error" in parsed:
                return {"status": "error",
                        "message": f"[call_id={cid}, tool={name}, sent={json.dumps(arguments)[:120]}] "
                                   + json.dumps(parsed["error"])[:300]
                                   + f" | toolbox exposes: {_inventory(tools)}"}
            result = parsed.get("result") or {}
            texts = [
                blk.get("text", "")
                for blk in (result.get("content") or [])
                if isinstance(blk, dict)
            ]
            joined = "\n".join(t for t in texts if t)
            if result.get("isError"):
                return {"status": "error",
                        "message": f"[call_id={cid}, tool={name}, sent={json.dumps(arguments)[:120]}] "
                                   + (joined or json.dumps(result)[:400])}
            return {"status": "ok", "text": joined or json.dumps(result)[:1500]}

    except httpx.HTTPStatusError as ex:
        return {"status": "error", "message": f"HTTP {ex.response.status_code}: {ex.response.text[:400]}"}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.error("Starburst query failed: %s", ex, exc_info=True)
        return {"status": "error", "message": f"{type(ex).__name__}: {ex}"}
