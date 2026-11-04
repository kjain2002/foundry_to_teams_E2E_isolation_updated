# Copyright (c) Microsoft. All rights reserved.
"""The custom tool the Copilot SDK can call: a simple per-conversation to-do list.

Tasks are persisted to a small JSON file under ``$HOME`` so they survive sandbox
idle/recycle. Foundry hosted agents give each session a **persistent ``$HOME``**:
its contents are preserved when the compute is deprovisioned after idle and
restored when the session resumes, so files written there outlive the container.
See https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#session-storage
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from copilot import Tool, define_tool
from pydantic import BaseModel, Field

import toolbox
import auth_retry

logger = logging.getLogger("github-copilot.tools")

# Persist under $HOME (durable across idle/recycle); fall back to cwd for local runs.
_HOME = os.environ.get("HOME") or "."
_STORE_DIR = Path(_HOME) / ".github-copilot" / "tasks"


class AddTaskParams(BaseModel):
    title: str = Field(description="The task description.")


class CompleteTaskParams(BaseModel):
    task_id: str = Field(description="The id of the task to mark done.")


class DeliverFileParams(BaseModel):
    path: str = Field(description="The absolute path of the file you created "
                                  "(with your shell / python tools) to send to "
                                  "the user for download.")


class QueryStarburstParams(BaseModel):
    sql: str = Field(description="The SQL to run against the data source, e.g. "
                                 "'SHOW CATALOGS' or 'SELECT ...'.")


class GetSkillParams(BaseModel):
    name: str = Field(default="", description="Optional skill name (partial ok). "
                                              "Leave empty to list available skills.")


class SearchToolboxParams(BaseModel):
    query: str = Field(description="Describe the capability needed for the user's request.")
    toolbox: str = Field(default="", description="Exact toolbox key from a previous search; empty searches all.")


class CallToolboxParams(BaseModel):
    toolbox: str = Field(description="Exact toolbox key returned by search_toolbox.")
    name: str = Field(description="Exact tool name returned by discovery, not a guessed name.")
    arguments: dict[str, Any] = Field(description="Arguments matching the discovered tool's input schema.")


class ReadToolboxResourceParams(BaseModel):
    toolbox: str = Field(description="Exact toolbox key shown by get_skill or search_toolbox.")
    uri: str = Field(description="Exact resource URI from the skill or tool result.")


class _NoParams(BaseModel):
    pass


# ── Per-turn UI outbox ─────────────────────────────────────────────────────────
# Copilot SDK tools are pure functions and cannot send activities (cards/files)
# back to Teams. When the model calls a tool that should render UI, the tool
# records the request here; ``main.py`` drains it after the turn and attaches the
# card/file to the streaming response (the reliable delivery path).
_OUTBOX: dict[str, list[dict[str, Any]]] = {}


def queue_ui(conversation_id: str, action: dict[str, Any]) -> None:
    _OUTBOX.setdefault(conversation_id, []).append(action)


def drain_ui(conversation_id: str) -> list[dict[str, Any]]:
    return _OUTBOX.pop(conversation_id, [])


# Per-turn Foundry call id (x-agent-foundry-call-id), captured in main.on_message
# and forwarded by query_starburst so the toolbox resolves the end user for
# per-user OAuth identity passthrough.
_CALL_IDS: dict[str, str | None] = {}


def set_call_id(conversation_id: str, call_id: str | None) -> None:
    _CALL_IDS[conversation_id] = call_id


def get_call_id(conversation_id: str) -> str | None:
    return _CALL_IDS.get(conversation_id)


def prepare_auth_result(conversation_id: str, result: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    for collection in ("toolboxes", "source_searches"):
        if collection in result:
            result = {**result, collection: [prepare_auth_result(conversation_id, item, operation)
                                             for item in result[collection]]}
    if result.get("status") != "consent":
        return result
    retry_id = auth_retry.stage(conversation_id, operation)
    queue_ui(conversation_id, {
        "type": "consent", "url": result.get("url"),
        "label": result.get("source") or result.get("toolbox") or toolbox.server_label(),
        "retry_id": retry_id,
    })
    return {"status": "consent", "toolbox": result.get("toolbox"), "source": result.get("source"),
            "retry_available": bool(retry_id),
            "message": "Sign-in card shown. After signing in, use its retry button or send a message. "
                       "The host will retry the saved operation; do not invent results or repeat successful actions."}


def needs_consent(result: dict[str, Any]) -> bool:
    return result.get("status") == "consent" or any(
        needs_consent(item) for collection in ("toolboxes", "source_searches")
        for item in result.get(collection, [])
    )


def execute_auth_operation(conversation_id: str, operation: dict[str, Any]) -> dict[str, Any]:
    kind = operation.get("kind")
    call_id = get_call_id(conversation_id)
    if kind == "sql":
        result = toolbox.run_query(operation["sql"], call_id=call_id)
    elif kind == "inventory":
        result = {"toolboxes": [toolbox.toolbox_inventory(key, call_id=call_id)
                                for key in toolbox.toolbox_servers()]}
    elif kind == "search":
        keys = [operation["toolbox"]] if operation.get("toolbox") else list(toolbox.toolbox_servers())
        result = {"toolboxes": [toolbox.toolbox_request(key, query=operation["query"], call_id=call_id)
                                for key in keys]}
    elif kind == "call":
        result = toolbox.toolbox_request(operation["toolbox"], name=operation["name"],
                                                arguments=operation["arguments"], call_id=call_id)
    elif kind == "resource":
        result = toolbox.toolbox_request(operation["toolbox"], uri=operation["uri"], call_id=call_id)
    else:
        return {"status": "error", "message": "Saved authorization retry has an unsupported operation."}
    return prepare_auth_result(conversation_id, result, operation)



_OOXML_EXTENSIONS = (".pptx", ".docx", ".xlsx")


def _ooxml_problem(path: str) -> str | None:
    """Return why an Office file would open as corrupt, or None if it looks sound.

    Catches the python-pptx template mistake: deleting template slides from the
    slide list without dropping their relationships leaves the old parts in the
    package, and new slides reuse their names, writing duplicate zip entries.
    """
    if not path.lower().endswith(_OOXML_EXTENSIONS):
        return None
    import collections
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return "not a valid zip/Office package"
    dups = sorted(n for n, c in collections.Counter(names).items() if c > 1)
    if dups:
        hint = (" When reusing a .pptx template, drop each removed slide's "
                "relationship with prs.part.drop_rel(sldId.rId) before removing "
                "its sldId." if path.lower().endswith(".pptx") else "")
        return f"duplicate package parts {', '.join(dups[:4])}.{hint}"
    return None


def _task_file(conversation_id: str) -> Path:
    # Sanitize the conversation id so it is safe to use as a file name.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id) or "default"
    return _STORE_DIR / f"{safe}.json"


def _load_tasks(conversation_id: str) -> list[dict[str, Any]]:
    path = _task_file(conversation_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        logger.warning("task store corrupt (%s) -> []", exc)
        return []


def _save_tasks(conversation_id: str, tasks: list[dict[str, Any]]) -> None:
    path = _task_file(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks), encoding="utf-8")


# ── Public helpers for the Adaptive Card UI (bypass the model, act on the store
#    directly from card button actions). ────────────────────────────────────────

def get_tasks(conversation_id: str) -> list[dict[str, Any]]:
    """Public accessor for the current tasks of a conversation."""
    return _load_tasks(conversation_id)


def add_task_direct(conversation_id: str, title: str) -> dict[str, Any] | None:
    """Add a task directly (no model). Returns the new task, or None if blank/dupe."""
    title = (title or "").strip()
    if not title:
        return None
    tasks = _load_tasks(conversation_id)
    for t in tasks:
        if not t["done"] and t["title"].casefold() == title.casefold():
            return None
    task = {"id": uuid.uuid4().hex[:8], "title": title, "done": False}
    tasks.append(task)
    _save_tasks(conversation_id, tasks)
    return task


def complete_task_direct(conversation_id: str, task_id: str) -> bool:
    """Mark a task done directly. Returns True if found."""
    tasks = _load_tasks(conversation_id)
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            _save_tasks(conversation_id, tasks)
            return True
    return False


def delete_task_direct(conversation_id: str, task_id: str) -> bool:
    """Delete a task directly. Returns True if removed."""
    tasks = _load_tasks(conversation_id)
    new = [t for t in tasks if t["id"] != task_id]
    if len(new) == len(tasks):
        return False
    _save_tasks(conversation_id, new)
    return True



def build_tools(conversation_id: str) -> list[Tool]:
    """Return the to-do tool set bound to ``conversation_id`` (file-backed)."""

    def _add_task(params: AddTaskParams, _inv: Any) -> str:
        title = (params.title or "").strip()
        tasks = _load_tasks(conversation_id)
        for t in tasks:  # idempotent: don't duplicate an open task
            if not t["done"] and t["title"].casefold() == title.casefold():
                return f"Task '{title}' is already on the list."
        task = {"id": uuid.uuid4().hex[:8], "title": title, "done": False}
        tasks.append(task)
        _save_tasks(conversation_id, tasks)
        return f"Added task '{title}' (id {task['id']})."

    def _list_tasks(_params: _NoParams, _inv: Any) -> str:
        # Render the interactive task board (drained + sent by main.py after the
        # turn). Return a terse instruction — NOT the task list — so the model
        # does not also enumerate the tasks in text (the card is the UI).
        queue_ui(conversation_id, {"type": "task_board"})
        tasks = _load_tasks(conversation_id)
        if not tasks:
            return ("Displayed an interactive task board to the user (it's empty). "
                    "Do not list tasks in text; just briefly invite them to add one.")
        return ("Displayed an interactive task board to the user showing their "
                f"{len(tasks)} task(s). Do not repeat or list the tasks in text; "
                "the card already shows them. Reply with at most a short sentence.")

    def _complete_task(params: CompleteTaskParams, _inv: Any) -> str:
        tasks = _load_tasks(conversation_id)
        for t in tasks:
            if t["id"] == params.task_id:
                t["done"] = True
                _save_tasks(conversation_id, tasks)
                return f"Marked '{t['title']}' as done."
        return f"No task with id '{params.task_id}'."

    def _deliver_file(params: DeliverFileParams, _inv: Any) -> str:
        # ChatGPT-style: the model creates the file itself using its built-in
        # shell / python tools (writing text directly, or using python-docx /
        # python-pptx / reportlab for Office / PDF), then calls this tool with
        # the path. We read the bytes after the turn and offer the file for
        # download via the Teams File Consent flow (see main._deliver_ui).
        path = (params.path or "").strip()
        if not path or not os.path.isfile(path):
            return (f"No file exists at '{path}'. Create the file first with your "
                    "shell/python tools, then call deliver_file with its path.")
        problem = _ooxml_problem(path)
        if problem:
            logger.warning("DELIVER_FILE_REJECTED path=%s problem=%s", path, problem)
            return (f"Not delivered: {os.path.basename(path)} is corrupt ({problem}). "
                    "Office will ask the user to repair it. Rebuild the file and call "
                    "deliver_file again.")
        queue_ui(conversation_id, {"type": "file", "path": path})
        return f"I've prepared **{os.path.basename(path)}** and will offer it for download."

    def _query_starburst(params: QueryStarburstParams, _inv: Any) -> str:
        # Call the linked toolbox ourselves so we can catch CONSENT_REQUIRED and
        # show the user the sign-in link (the SDK's MCP mount hides that error).
        sql = (params.sql or "").strip()
        if not sql:
            return "Provide a SQL statement to run (e.g. 'SHOW CATALOGS')."
        res = toolbox.run_query(sql, call_id=_CALL_IDS.get(conversation_id))
        status = res.get("status")
        if status == "consent":
            prepare_auth_result(conversation_id, res, {"kind": "sql", "sql": sql})
            return ("The user isn't connected to the data source yet. I've shown "
                    "them a sign-in card to authorize access. Ask them to click it "
                    "and then use its retry button or send a message. The host will "
                    "retry the saved query. Do not invent results.")
        if status == "ok":
            return f"Query result:\n{res.get('text')}"
        return ("The Starburst query failed. Show this diagnostic line to the user "
                f"verbatim (it helps debugging): {res.get('message')}")

    tools: list[Tool] = [
        define_tool("add_task", description="Add a task / to-do item.",
                    handler=_add_task, params_type=AddTaskParams),
        define_tool("list_tasks",
                    description="Show the user's to-do list. Use this whenever the "
                                "user wants to see, list, or review their tasks.",
                    handler=_list_tasks, params_type=_NoParams),
        define_tool("complete_task", description="Mark a task as done by its id.",
                    handler=_complete_task, params_type=CompleteTaskParams),
        define_tool("deliver_file",
                    description="Send a file you created to the user as a "
                                "download. First create the file yourself using "
                                "your shell / python tools (write text directly "
                                "for .txt/.md/.csv/.json/.html/code, or use "
                                "python-docx / python-pptx / reportlab for .docx / "
                                ".pptx / .pdf), then call this tool with the file's "
                                "path. Use this whenever the user asks you to write, "
                                "generate, or produce a file, document, essay, "
                                "report, or slides. You MUST actually create the "
                                "file and call this tool — never claim you attached "
                                "a file without doing so. Do not generate images.",
                    handler=_deliver_file, params_type=DeliverFileParams),
    ]
    if toolbox.is_available():
        def _list_toolbox_tools(_params: _NoParams, _inv: Any) -> str:
            return json.dumps(execute_auth_operation(conversation_id, {"kind": "inventory"}), ensure_ascii=False)

        def _search_toolbox(params: SearchToolboxParams, _inv: Any) -> str:
            return json.dumps(execute_auth_operation(conversation_id, {"kind": "search", **params.model_dump()}), ensure_ascii=False)

        def _call_toolbox(params: CallToolboxParams, _inv: Any) -> str:
            return json.dumps(execute_auth_operation(conversation_id, {"kind": "call", **params.model_dump()}), ensure_ascii=False)

        def _read_toolbox_resource(params: ReadToolboxResourceParams, _inv: Any) -> str:
            return json.dumps(execute_auth_operation(conversation_id, {"kind": "resource", **params.model_dump()}), ensure_ascii=False)

        tools.extend([
            define_tool("list_toolbox_tools",
                        description="List configured MCP sources and discover their tools across all linked "
                                    "toolboxes. Use this when asked what tools/connectors are available. "
                                    "Include configured toolbox built-ins such as Code Interpreter separately "
                                    "from this helper's host tools; retain all source and consent/error statuses. "
                                    "A generic keyword search alone is not a full inventory.",
                        handler=_list_toolbox_tools, params_type=_NoParams),
            define_tool("search_toolbox",
                        description="Discover tools across all linked toolboxes/MCP servers. Search for the "
                                    "user's actual task, including non-SQL tasks. Results identify each "
                                    "toolbox, exact tool names and argument schemas. Discover before calling.",
                        handler=_search_toolbox, params_type=SearchToolboxParams),
            define_tool("call_toolbox_tool",
                        description="Invoke a tool discovered by search_toolbox using its exact toolbox key, "
                                    "name and schema-matching arguments. Consent is shown as a sign-in card.",
                        handler=_call_toolbox, params_type=CallToolboxParams),
            define_tool("read_toolbox_resource",
                        description="Read a skill's supporting resource or another MCP resource by its exact URI. "
                                    "Returns text or base64 content as provided by the toolbox.",
                        handler=_read_toolbox_resource, params_type=ReadToolboxResourceParams),
        ])
        tools.append(define_tool(
            "query_starburst",
            description="Run a SQL query against the linked data source "
                        "(e.g. Starburst). Use this whenever the user asks about "
                        "data. This uses the same toolbox MCP route, not an independent "
                        "non-MCP fallback. Use it for "
                        "catalogs, schemas, tables, or data. Pass the SQL in `sql` "
                        "(start with 'SHOW CATALOGS' if unsure). If the user needs "
                        "to authorize first, this tool shows them a sign-in card — "
                        "never fabricate query results.",
            handler=_query_starburst, params_type=QueryStarburstParams))

        def _get_skill(params: GetSkillParams, _inv: Any) -> str:
            # One line per model call, so each turn shows whether a skill was loaded.
            name = (params.name or "").strip() or None
            result = toolbox.get_skill_markdown(name)
            logger.info("SKILL_TOOL_CALLED conversation_id=%s name=%s returned_chars=%d",
                        conversation_id, name, len(result))
            return result

        tools.append(define_tool(
            "get_skill",
            description="Load a skill published on the linked toolbox and return "
                        "its full instructions (markdown). Call this before a task "
                        "matching a skill in the system-prompt index and follow it; also call it "
                        "when the user asks to see a skill or a copy of its "
                        "markdown. Don't call it for unrelated requests. Leave "
                        "`name` empty to list available skills.",
            handler=_get_skill, params_type=GetSkillParams))
    return tools
