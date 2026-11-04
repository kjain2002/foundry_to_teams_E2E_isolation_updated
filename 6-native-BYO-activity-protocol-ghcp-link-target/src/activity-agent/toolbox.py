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
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit
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
    _SERVERS = _TARGET.mcp_servers
    _SERVER = _SERVERS[0] if _SERVERS else None
except Exception as _ex:  # pylint: disable=broad-exception-caught
    logger.warning("Could not load target toolbox (%s); Starburst tool disabled.", _ex)
    _SERVERS = []
    _SERVER = None


def is_available() -> bool:
    """True when the linked target agent carries a toolbox/MCP server to query."""
    return _SERVER is not None


def server_label() -> str:
    return (_SERVER or {}).get("label", "toolbox")


def toolbox_servers() -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    for index, server in enumerate(_SERVERS, start=1):
        label = server.get("label") or "mcp"
        key = label if label not in servers else f"{label}-{index}"
        while key in servers:
            key += "-duplicate"
        servers[key] = server
    return servers


class _ToolboxFailure(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__(result.get("message") or result.get("status"))
        self.result = result


def _is_unauthorized(value: Any, *, error_context: bool = False) -> bool:
    if isinstance(value, dict):
        error_context = error_context or bool(value.get("isError"))
        for key in ("code", "status", "statusCode", "status_code"):
            if str(value.get(key, "")).lower() in ("401", "unauthorized"):
                return True
        for key in ("error", "detail", "details", "result", "message"):
            if _is_unauthorized(value.get(key), error_context=error_context or key == "error"):
                return True
        return any(_is_unauthorized(block.get("text"), error_context=error_context) for block in value.get("content", [])
                   if isinstance(block, dict))
    if isinstance(value, list):
        return any(_is_unauthorized(item, error_context=error_context) for item in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return error_context and bool(re.search(r"\b401\b|unauthori[sz]ed", value, re.IGNORECASE))
        return _is_unauthorized(parsed, error_context=error_context) if isinstance(parsed, (dict, list)) else False
    return False


def _checked_response(response) -> dict[str, Any]:
    consent = _consent_url(response.text)
    if consent:
        raise _ToolboxFailure({"status": "consent", "url": consent})
    if response.status_code == 401:
        raise _ToolboxFailure({"status": "error", "auth_required": True,
                               "message": "The tool endpoint returned HTTP 401 Unauthorized."})
    response.raise_for_status()
    parsed = _parse(response.text)
    if not parsed:
        raise _ToolboxFailure({"status": "error", "message": "Empty or invalid MCP response."})
    if _is_unauthorized(parsed):
        raise _ToolboxFailure({"status": "error", "auth_required": True,
                               "message": "The tool reported an authentication failure (401 Unauthorized)."})
    if "error" in parsed:
        raise _ToolboxFailure({"status": "error", "message": json.dumps(parsed["error"])})
    result = parsed.get("result") or {}
    if result.get("isError"):
        raise _ToolboxFailure({"status": "error", "message": json.dumps(result)})
    return result


@contextmanager
def _connect_toolbox(server: dict, call_id: str | None = None):
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if call_id:
        headers["x-agent-foundry-call-id"] = call_id
    with httpx.Client(timeout=90, headers=headers) as client:
        response = client.post(server["url"], json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "activity-agent", "version": "1.0"}},
        })
        _checked_response(response)
        session_id = response.headers.get("mcp-session-id")
        session_headers = {"mcp-session-id": session_id} if session_id else {}
        initialized = client.post(server["url"], headers=session_headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })
        initialized.raise_for_status()
        yield client, session_headers


def _rpc(client, server: dict, headers: dict, method: str, params: dict) -> dict:
    response = client.post(server["url"], headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": method, "params": params,
    })
    return _checked_response(response)


def _list_tools(client, server: dict, headers: dict) -> list[dict]:
    tools = []
    params: dict[str, Any] = {}
    seen = set()
    while True:
        result = _rpc(client, server, headers, "tools/list", params)
        tools.extend(tool for tool in result.get("tools", []) if isinstance(tool, dict))
        cursor = result.get("nextCursor")
        if not cursor:
            return tools
        if cursor in seen:
            raise ValueError("MCP tools/list repeated a pagination cursor.")
        seen.add(cursor)
        params = {"cursor": cursor}


def _recover_consent(server: dict, call_id: str | None, failure: dict[str, Any], query: str | None = None) -> dict[str, Any]:
    if not failure.get("auth_required"):
        return failure
    try:
        with _connect_toolbox(server, call_id) as (client, headers):
            listed = _list_tools(client, server, headers)
            names = {tool.get("name", "").lower() for tool in listed}
            if query and _has_search_gateway(names):
                search = next(tool["name"] for tool in listed if tool.get("name", "").lower() in ("tool_search", "search"))
                _rpc(client, server, headers, "tools/call", {
                    "name": search, "arguments": {"query": query, "limit": 10},
                })
    except _ToolboxFailure as exc:
        if exc.result.get("status") == "consent":
            return exc.result
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("Consent discovery probe failed; original authentication error retained.")
    return {**failure, "message": failure["message"] +
            " The read-only toolbox probe did not provide a consent URL; connection or backend credentials may need repair."}


def _has_search_gateway(names: set[str]) -> bool:
    return bool(names & {"tool_search", "search"}) and bool(names & {"call_tool", "invoke_tool"})


def toolbox_request(
    toolbox: str, query: str | None = None, name: str | None = None,
    arguments: dict[str, Any] | None = None, call_id: str | None = None,
    uri: str | None = None,
) -> dict[str, Any]:
    server = toolbox_servers().get(toolbox)
    if server is None:
        return {"status": "error", "message": "Unknown toolbox. Search linked toolboxes first."}
    try:
        with _connect_toolbox(server, call_id) as (client, headers):
            if uri is not None:
                result = _rpc(client, server, headers, "resources/read", {"uri": uri})
                return {"status": "ok", "toolbox": toolbox, "result": result}
            tools = _list_tools(client, server, headers)
            names = {tool.get("name", "").lower() for tool in tools}
            facade = _has_search_gateway(names)
            if name is not None:
                approval = server.get("require_approval", "never")
                if approval != "never":
                    raise ValueError("This toolbox requires tool approval; the bridge cannot bypass that policy.")
                if facade:
                    invoke = next((tool["name"] for tool in tools
                                   if tool.get("name", "").lower() in ("call_tool", "invoke_tool")), None)
                    if not invoke:
                        raise ValueError("Toolbox facade has no invocation tool.")
                    params = {"name": invoke, "arguments": {"name": name, "arguments": arguments or {}}}
                else:
                    if name not in {tool.get("name") for tool in tools}:
                        raise ValueError("Tool is no longer listed by this toolbox; search again.")
                    params = {"name": name, "arguments": arguments or {}}
                logger.info("TOOLBOX_CALL_START toolbox=%s tool=%s via=%s call_id_present=%s",
                            toolbox, name, params["name"], bool(call_id))
                result = _rpc(client, server, headers, "tools/call", params)
                logger.info("TOOLBOX_CALL_COMPLETE toolbox=%s tool=%s", toolbox, name)
            elif facade:
                search = next((tool["name"] for tool in tools if "search" in tool.get("name", "").lower()), None)
                if not search:
                    raise ValueError("Toolbox facade has no search tool.")
                result = _rpc(client, server, headers, "tools/call", {
                    "name": search, "arguments": {"query": query or "available tools", "limit": 10},
                })
            else:
                result = {"tools": tools}
            response = {"status": "ok", "toolbox": toolbox, "result": result}
            if name is None and facade:
                response.update({
                    "query": query or "available tools", "inventory_complete": False,
                    "discovery_note": "These are keyword search matches, not a complete toolbox inventory. "
                                      "Search separately for each requested capability or connector. "
                                      "Do not claim unreturned tools are absent.",
                })
            return response
    except _ToolboxFailure as exc:
        return {**_recover_consent(server, call_id, exc.result, name or query), "toolbox": toolbox}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Toolbox %s request failed: %s", toolbox, exc)
        return {"status": "error", "toolbox": toolbox, "message": str(exc)}


def toolbox_inventory(toolbox: str, call_id: str | None = None) -> dict[str, Any]:
    server = toolbox_servers().get(toolbox)
    if server is None:
        return {"status": "error", "toolbox": toolbox, "message": "Unknown linked toolbox."}
    try:
        with _connect_toolbox(server, call_id) as (client, headers):
            listed = _list_tools(client, server, headers)
            names = {tool.get("name", "").lower() for tool in listed}
            if not _has_search_gateway(names):
                return {"status": "ok", "toolbox": toolbox, "inventory_complete": True,
                        "result": {"tools": listed}}
            parts = urlsplit(server["url"])
            if "/toolboxes/" not in parts.path or not parts.path.endswith("/mcp"):
                return {"status": "partial", "toolbox": toolbox, "inventory_complete": False,
                        "message": "This MCP server exposes search, not an enumerable inventory. "
                                   "Use targeted search_toolbox queries; do not claim other tools are absent."}
            definition_url = urlunsplit((parts.scheme, parts.netloc, parts.path[:-4], parts.query, ""))
            response = client.get(definition_url)
            response.raise_for_status()
            definition = response.json()
            if "/versions/" not in parts.path:
                default_version = definition.get("default_version")
                if not default_version:
                    raise ValueError("Toolbox has no published default version.")
                version_path = parts.path[:-4] + "/versions/" + quote(str(default_version), safe="")
                version_url = urlunsplit((parts.scheme, parts.netloc, version_path, parts.query, ""))
                response = client.get(version_url)
                response.raise_for_status()
                definition = response.json()
            declared = definition.get("tools")
            if not isinstance(declared, list):
                raise ValueError("Toolbox definition did not contain a tools list.")
            sources = []
            builtins = []
            for tool in declared:
                if not isinstance(tool, dict):
                    continue
                if tool.get("type") == "mcp":
                    source = tool.get("server_label") or tool.get("name")
                    if not source:
                        raise ValueError("A configured MCP source has no label or name.")
                    sources.append({"source": source, "connection": tool.get("project_connection_id"),
                                    "endpoint": tool.get("server_url")})
                elif tool.get("type") != "toolbox_search":
                    builtins.append({"name": tool.get("name") or tool.get("type"), "type": tool.get("type")})
            search = next((tool["name"] for tool in listed if "search" in tool.get("name", "").lower()), None)
            if not search:
                raise ValueError("Toolbox facade has no search tool.")
            searches = []
            for source in sources:
                try:
                    result = _rpc(client, server, headers, "tools/call", {
                        "name": search, "arguments": {"query": source["source"], "limit": 10},
                    })
                    searches.append({"status": "ok", "toolbox": toolbox, "source": source["source"],
                                     "result": result})
                except _ToolboxFailure as exc:
                    searches.append({**_recover_consent(server, call_id, exc.result, source["source"]),
                                     "toolbox": toolbox, "source": source["source"]})
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    searches.append({"status": "error", "toolbox": toolbox, "source": source["source"],
                                     "message": str(exc)})
            logger.info("TOOLBOX_INVENTORY toolbox=%s sources=%s statuses=%s call_id_present=%s",
                        toolbox, [source["source"] for source in sources],
                        [result["status"] for result in searches], bool(call_id))
            return {
                "status": "ok" if all(result["status"] == "ok" for result in searches) else "partial",
                "toolbox": toolbox, "inventory_complete": False,
                "toolbox_version": definition.get("version"),
                "configured_mcp_sources": sources, "builtin_tools": builtins,
                "source_searches": searches,
                "discovery_note": "Configured MCP sources come from the attached toolbox definition, "
                                  "not keyword guesses. Built-in tools such as code_interpreter are not "
                                  "custom MCP connectors, but are still toolbox tools and must be shown "
                                  "in the toolbox tool list, separately from the helper's local host tools. "
                                  "Report each configured source and its discovery "
                                  "status, even if it returned no matching tools or requires consent. "
                                  "Source searches are keyword matches, not a guaranteed exhaustive tool list.",
            }
    except _ToolboxFailure as exc:
        return {**_recover_consent(server, call_id, exc.result), "toolbox": toolbox}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Toolbox %s inventory failed: %s", toolbox, exc)
        return {"status": "error", "toolbox": toolbox, "inventory_complete": False, "message": str(exc)}


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
        "params": {"name": search_name, "arguments": {"query": "execute SQL query statement", "limit": 10}},
    }
    rs = c.post(url, json=search, headers=h2)
    if _consent_url(rs.text):
        return {"status": "consent", "url": _consent_url(rs.text)}
    _checked_response(rs)
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
    _checked_response(rc)
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
_SKILLS_CACHE: dict[str, list[dict]] = {}
_SKILLS_FETCHED_AT: dict[str, float] = {}
_SKILLS_CACHE_SECONDS = 60


def _mcp(c, url: str, h2: dict, method: str, params: dict) -> dict:
    r = c.post(url, json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params}, headers=h2)
    return _parse(r.text) or {}


def fetch_skills() -> list[dict]:
    """Read skills from every linked toolbox; retry failures independently."""
    out: list[dict] = []
    for key, server in toolbox_servers().items():
        if key in _SKILLS_CACHE and time.monotonic() - _SKILLS_FETCHED_AT.get(key, 0) < _SKILLS_CACHE_SECONDS:
            out.extend(_SKILLS_CACHE[key])
            continue
        skills: list[dict] = []
        try:
            with _connect_toolbox(server) as (client, headers):
                params: dict[str, Any] = {}
                seen = set()
                while True:
                    listed = _rpc(client, server, headers, "skills/list", params)
                    for skill in listed.get("skills") or []:
                        if not isinstance(skill, dict) or not skill.get("uri"):
                            continue
                        frontmatter = skill.get("frontmatter") or {}
                        result = _rpc(client, server, headers, "resources/read", {"uri": skill["uri"]})
                        markdown = "\n".join(block.get("text", "") for block in result.get("contents", [])
                                             if isinstance(block, dict))
                        skills.append({
                            "name": f"{key}/{frontmatter.get('name') or skill['uri']}",
                            "description": frontmatter.get("description", ""),
                            "toolbox": key, "uri": skill["uri"], "markdown": markdown,
                            "resources": skill.get("resources") or [],
                        })
                    cursor = listed.get("nextCursor")
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError("MCP skills/list repeated a pagination cursor.")
                    seen.add(cursor)
                    params = {"cursor": cursor}
            if all(skill.get("markdown") for skill in skills):
                _SKILLS_CACHE[key] = skills
                _SKILLS_FETCHED_AT[key] = time.monotonic()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Skills unavailable from toolbox %s: %s", key, exc)
        out.extend(skills)
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
        matches = [skill for skill in skills if name.lower() in skill.get("name", "").lower()]
        if len(matches) == 1:
            skill = matches[0]
            return (f"Toolbox: {skill['toolbox']}\nResource: {skill['uri']}\n"
                    f"Supporting resources (read with read_toolbox_resource): "
                    f"{json.dumps(skill.get('resources') or [])}\n\n{skill.get('markdown') or ''}")
    if len(skills) == 1:
        return get_skill_markdown(skills[0]["name"]) if not name else "No matching skill."
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
            _checked_response(r)
            sid = r.headers.get("mcp-session-id")
            h2 = {"mcp-session-id": sid} if sid else {}

            # List tools; a connection needing auth reports CONSENT_REQUIRED here.
            rl = c.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h2)
            consent = _consent_url(rl.text)
            if consent:
                return {"status": "consent", "url": consent}
            _checked_response(rl)
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
            if _has_search_gateway(lower):
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
            _checked_response(rc)
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

    except _ToolboxFailure as ex:
        return _recover_consent(_SERVER, call_id, ex.result, "execute SQL query statement")
    except httpx.HTTPStatusError as ex:
        if ex.response.status_code == 401:
            return _recover_consent(_SERVER, call_id, {"status": "error", "auth_required": True,
                                                      "message": "The tool endpoint returned HTTP 401 Unauthorized."},
                                    "execute SQL query statement")
        return {"status": "error", "message": f"HTTP {ex.response.status_code}: {ex.response.text[:400]}"}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.error("Starburst query failed: %s", ex, exc_info=True)
        return {"status": "error", "message": f"{type(ex).__name__}: {ex}"}
