# Copyright (c) Microsoft. All rights reserved.
"""Resolve a *target agent* definition and fold it into this helper agent.

The goal: publish **one** hosted activity agent to Teams that *behaves like* an
existing (target) agent — adopting its **instructions**, **model**, and **tool
intent** — without standing up a second agent or routing to a second session.

Two ways to point at a target (first match wins; if neither is set the helper
keeps its own built-in persona unchanged):

1. **A local file** — set ``TARGET_AGENT_FILE`` to a ``.yaml``/``.json`` path, or
   drop a ``target-agent.yaml`` / ``target-agent.json`` next to this module.
   Developer-friendly and version-controlled. Schema::

       name: My Agent
       model: gpt-4o            # optional; overrides AZURE_AI_MODEL_DEPLOYMENT_NAME
       instructions: |          # the target agent's system prompt
         You are ...
       tools:                   # optional; same shape as a Foundry agent's tools
         - type: code_interpreter
         - type: file_search
         - type: mcp
           server_label: my-toolbox

2. **An existing Foundry agent** — set ``TARGET_AGENT_ID`` (the agent name/id) and
   the helper fetches that agent's instructions/model/tools from the Foundry
   project (``FOUNDRY_PROJECT_ENDPOINT``) at startup via ``azure-ai-projects``.

Tool handling is deliberate: ``code_interpreter`` / ``file_search`` /
file-reading tools on the target are **not** proxied — the helper already has
native GitHub Copilot SDK tools for reading shared files, running python to
build documents, and delivering downloads, so those capabilities are satisfied
locally. Any *other* target tool (a custom function, an MCP server / toolbox, an
OpenAPI tool, grounding, …) can't be executed here without the target's hosted
runtime, so it is **surfaced to the model** (named in the system prompt) rather
than silently dropped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("github-copilot.target")

# Target tool types already covered by the helper's native GHCP capabilities.
# We do NOT proxy these — the built-in shell/python/file tools + deliver_file do
# the same job in-process.
_NATIVE_EQUIVALENT_TYPES = {
    "code_interpreter",
    "code-interpreter",
    "codeinterpreter",
    "file_search",
    "file-search",
    "filesearch",
    "file_reader",
    "file-reader",
}

_DEFAULT_FILE_NAMES = ("target-agent.yaml", "target-agent.yml", "target-agent.json")


@dataclass
class TargetAgent:
    """A normalized view of the target agent the helper should emulate."""

    name: str | None = None
    instructions: str | None = None
    model: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    native_capabilities: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    extra_tools: list[str] = field(default_factory=list)
    source: str = "none"

    @property
    def configured(self) -> bool:
        """True when a target was actually resolved (vs. the no-op default)."""
        return bool(self.instructions or self.model or self.tools)

    def model_name(self, fallback: str) -> str:
        """The deployment the SDK should use — target's model, else the fallback."""
        return self.model or fallback

    def system_message(self, fallback: str) -> str:
        """Effective system prompt — a template, not an either/or.

        Always keeps the helper's own personality (``fallback``) as the default,
        then plugs the target agent's prompt in as the *functional purpose
        customized for this published agent*, then a capabilities footer. An
        unconfigured target returns ``fallback`` verbatim (helper unchanged).
        """
        if not self.configured:
            return fallback
        parts = [fallback.rstrip()]
        if self.instructions:
            parts.append(
                "--- Functional purpose (customized for this published agent) ---\n"
                "Keep the assistant personality described above as your default. "
                "For THIS deployment your functional purpose is defined by the "
                "target agent below; adopt its goals, domain, and behavior:\n\n"
                + self.instructions.strip()
            )
        parts.append(self._capability_footer())
        return "\n\n".join(parts)

    def _capability_footer(self) -> str:
        lines = [
            "--- Runtime capabilities (host-provided) ---",
            "You run inside Microsoft Teams / Microsoft 365 Copilot. The host gives "
            "you native tools: read files the user shares in chat, run shell/python "
            "in your workspace to create files, and send a file back for download by "
            "calling the deliver_file tool. Use these for reading shared files and "
            "for generating documents (write .txt/.md/.csv/.json/.html/code directly; "
            "for .docx/.pptx/.pdf pip-install python-docx/python-pptx/reportlab at "
            "runtime, build the file, then call deliver_file). Do this instead of any "
            "code-interpreter or file-search tool. You cannot generate images.",
        ]
        if self.mcp_servers:
            labels = ", ".join(s["label"] for s in self.mcp_servers)
            lines.append(
                "You also have the target agent's tools, callable here via MCP: "
                + labels + ". Use them for their domain tasks (e.g. data queries)."
            )
        if self.extra_tools:
            lines.append(
                "The source agent also declares these tools, which may not be "
                "callable in this host: " + ", ".join(self.extra_tools) + ". If you "
                "cannot call one, say so briefly rather than pretending to."
            )
        return "\n".join(lines)


def _classify_tools(
    tools: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Split target tools into native-covered, callable MCP servers, and extras."""
    native: list[str] = []
    mcp: list[dict[str, Any]] = []
    extra: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "").strip().lower()
        if ttype in _NATIVE_EQUIVALENT_TYPES:
            native.append(ttype)
            continue
        # MCP servers / Foundry toolboxes can be mounted on the Copilot SDK session.
        if ttype == "mcp" and tool.get("server_url"):
            entry: dict[str, Any] = {
                "label": str(tool.get("server_label") or "mcp"),
                "url": str(tool["server_url"]),
            }
            # A Foundry toolbox tool carries the project connection that holds its
            # OAuth identity-passthrough config; keep it so the host can auth the
            # MCP call as the user (the toolbox's connection) rather than only the
            # app bearer. Without it the downstream toolbox auth/consent can't run.
            conn = (
                tool.get("project_connection_id")
                or tool.get("connection_id")
                or tool.get("connectionId")
            )
            if conn:
                entry["project_connection_id"] = str(conn)
            approval = tool.get("require_approval")
            if approval is not None:
                entry["require_approval"] = approval
            mcp.append(entry)
            continue
        # Prefer a human-friendly label for the surfaced extras.
        label = (
            tool.get("server_label")
            or tool.get("name")
            or (tool.get("function") or {}).get("name")
            or ttype
            or "tool"
        )
        extra.append(str(label))
    return native, mcp, extra


def _from_mapping(data: dict[str, Any], source: str) -> TargetAgent:
    instructions = (
        data.get("instructions")
        or data.get("system_message")
        or data.get("systemMessage")
    )
    model = data.get("model") or data.get("model_deployment") or data.get("deployment")
    raw_tools = data.get("tools") or []
    tools = [t for t in raw_tools if isinstance(t, dict)]
    native, mcp, extra = _classify_tools(tools)
    target = TargetAgent(
        name=data.get("name"),
        instructions=instructions.strip() if isinstance(instructions, str) else None,
        model=str(model).strip() if model else None,
        tools=tools,
        native_capabilities=native,
        mcp_servers=mcp,
        extra_tools=extra,
        source=source,
    )
    return target


def _load_file(path: Path) -> TargetAgent:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                f"Reading {path.name} needs PyYAML (add 'PyYAML' to requirements.txt)."
            ) from exc
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Target agent file {path} must be a mapping at the top level.")
    return _from_mapping(data, source=f"file:{path.name}")


def _discover_file() -> Path | None:
    explicit = os.environ.get("TARGET_AGENT_FILE", "").strip()
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
        return p if p.is_file() else None
    here = Path(__file__).resolve().parent
    for name in _DEFAULT_FILE_NAMES:
        candidate = here / name
        if candidate.is_file():
            return candidate
    return None


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return tool
    for attr in ("as_dict", "model_dump"):
        fn = getattr(tool, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:  # pylint: disable=broad-exception-caught
                pass
    ttype = getattr(tool, "type", None)
    return {"type": ttype} if ttype else {}


def _load_foundry_agent(agent_id: str) -> TargetAgent:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError(
            "TARGET_AGENT_ID is set but FOUNDRY_PROJECT_ENDPOINT is missing."
        )
    try:
        from azure.ai.projects import AIProjectClient  # type: ignore
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "Fetching a target by TARGET_AGENT_ID needs 'azure-ai-projects' "
            "(add it to requirements.txt), or use a local target-agent file instead."
        ) from exc

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    agent = client.agents.get_agent(agent_id)
    tools = [_tool_to_dict(t) for t in (getattr(agent, "tools", None) or [])]
    data: dict[str, Any] = {
        "name": getattr(agent, "name", None) or agent_id,
        "instructions": getattr(agent, "instructions", None),
        "model": getattr(agent, "model", None),
        "tools": [t for t in tools if t],
    }
    return _from_mapping(data, source=f"foundry:{agent_id}")


def load() -> TargetAgent:
    """Resolve the target agent from a local file or a Foundry agent reference.

    Never raises for a *missing* target — returns an empty ``TargetAgent`` so the
    helper falls back to its own persona. Only raises when a target is explicitly
    requested but cannot be parsed/fetched, so misconfiguration is loud.
    """
    file_path = _discover_file()
    if file_path is not None:
        target = _load_file(file_path)
        logger.info(
            "Target agent loaded from %s (model=%s, native=%s, extra_tools=%s)",
            target.source, target.model, target.native_capabilities, target.extra_tools,
        )
        return target

    agent_id = os.environ.get("TARGET_AGENT_ID", "").strip()
    if agent_id:
        target = _load_foundry_agent(agent_id)
        logger.info(
            "Target agent fetched from %s (model=%s, native=%s, extra_tools=%s)",
            target.source, target.model, target.native_capabilities, target.extra_tools,
        )
        return target

    logger.info("No target agent configured; using the helper's built-in persona.")
    return TargetAgent()
