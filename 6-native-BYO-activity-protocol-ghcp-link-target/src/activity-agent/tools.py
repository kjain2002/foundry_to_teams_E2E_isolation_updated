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


class CallToolboxToolParams(BaseModel):
    name: str = Field(description="The exact tool name, as returned by list_toolbox_tools.")
    arguments_json: str = Field(default="{}",
                                description="The tool's arguments as a JSON object string, "
                                            "matching the tool's input_schema.")


class GetSkillParams(BaseModel):
    name: str = Field(default="", description="Optional skill name (partial ok). "
                                              "Leave empty to list available skills.")


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
# and forwarded by the toolbox tools so the toolbox resolves the end user for
# per-user OAuth identity passthrough.
_CALL_IDS: dict[str, str | None] = {}


def set_call_id(conversation_id: str, call_id: str | None) -> None:
    _CALL_IDS[conversation_id] = call_id


def get_call_id(conversation_id: str) -> str | None:
    return _CALL_IDS.get(conversation_id)



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

    def _consent_reply(res: dict[str, Any]) -> str | None:
        """Show the sign-in card when the toolbox demands consent; else None."""
        if res.get("status") != "consent":
            return None
        queue_ui(conversation_id, {
            "type": "consent",
            "url": res.get("url"),
            "label": toolbox.server_label(),
        })
        return ("The user isn't connected to this tool's data source yet. I've shown "
                "them a sign-in card to authorize access. Ask them to click it and "
                "then ask again. Do not invent results.")

    def _list_toolbox_tools(_params: _NoParams, _inv: Any) -> str:
        # Call the toolbox ourselves so we can catch CONSENT_REQUIRED and show the
        # user the sign-in link (the SDK's MCP mount hides that error).
        res = toolbox.list_tools(call_id=_CALL_IDS.get(conversation_id))
        consent = _consent_reply(res)
        if consent:
            return consent
        if res.get("status") == "ok":
            return json.dumps(res.get("tools"), indent=1)[:12000]
        return ("Listing the toolbox tools failed. Show this diagnostic line to the "
                f"user verbatim (it helps debugging): {res.get('message')}")

    def _call_toolbox_tool(params: CallToolboxToolParams, _inv: Any) -> str:
        try:
            arguments = json.loads(params.arguments_json or "{}")
        except ValueError as ex:
            return f"arguments_json is not valid JSON ({ex}). Fix it and call again."
        if not isinstance(arguments, dict):
            return "arguments_json must be a JSON object."
        res = toolbox.call_tool(params.name, arguments, call_id=_CALL_IDS.get(conversation_id))
        consent = _consent_reply(res)
        if consent:
            return consent
        if res.get("status") == "ok":
            return f"Tool result:\n{res.get('text')}"
        return ("The toolbox tool call failed. Show this diagnostic line to the user "
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
        tools.append(define_tool(
            "list_toolbox_tools",
            description="List the tools available on the linked toolbox (name, "
                        "description, input_schema). Call this first when the user "
                        "asks for something one of those tools could do, then call "
                        "call_toolbox_tool. If the user must authorize a data source "
                        "first, this shows them a sign-in card — never fabricate "
                        "tool results.",
            handler=_list_toolbox_tools, params_type=_NoParams))
        tools.append(define_tool(
            "call_toolbox_tool",
            description="Call one tool on the linked toolbox by its exact name "
                        "(from list_toolbox_tools) with arguments_json matching its "
                        "input_schema. If the toolbox only offers a search tool and "
                        "a call/invoke tool, use those the same way. If the user must "
                        "authorize first, this shows them a sign-in card — never "
                        "fabricate tool results.",
            handler=_call_toolbox_tool, params_type=CallToolboxToolParams))

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
                        "its full instructions (markdown). Before starting a task "
                        "that a toolbox skill might cover (for example generating a "
                        "particular kind of document), call this with an empty "
                        "`name` to see the skills and when to use each, then call it "
                        "again with the matching name and follow what it returns. "
                        "Also call it when the user asks to see a skill. Don't call "
                        "it for unrelated requests.",
            handler=_get_skill, params_type=GetSkillParams))
    return tools
