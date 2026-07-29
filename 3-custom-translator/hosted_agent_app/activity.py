"""Parse the Foundry / OpenAI Responses API output into typed activity items.

The golden rule enforced here: **evidence over inference.** We only report
that a tool ran when there is a corresponding activity *item* in the raw
Responses output. We never conclude Code Interpreter ran because the assistant
*text* claims it did.

Recognised item types include (non-exhaustive — unknowns are preserved raw):
    - function_call            (a tool / MCP function invocation)
    - function_call_output     (the result returned to the model)
    - mcp_call / mcp_list_tools (hosted MCP surface, e.g. an MCP data tool)
    - code_interpreter_call    (Code Interpreter executed)
    - oauth_consent_request    (user must authorise a tool)
    - message / output items   (assistant text)
    - error / *_error items
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _get(item: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-dict getter (Responses items may be SDK objects or dicts)."""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


@dataclass
class ActivityItem:
    """One normalised item from ``response.output[]``."""

    type: str
    id: str | None = None
    status: str | None = None
    name: str | None = None
    server_label: str | None = None
    arguments: Any = None
    output: Any = None
    text: str | None = None
    error: str | None = None
    raw: Any = field(default=None, repr=False)

    # --- classification helpers ---------------------------------------------
    @property
    def is_function_call(self) -> bool:
        return self.type in {"function_call", "mcp_call"}

    @property
    def is_function_call_output(self) -> bool:
        return self.type in {"function_call_output", "mcp_call_output"}

    @property
    def is_code_interpreter(self) -> bool:
        return self.type == "code_interpreter_call"

    @property
    def is_consent_request(self) -> bool:
        return self.type in {"oauth_consent_request", "mcp_approval_request"}

    @property
    def is_error(self) -> bool:
        return self.type == "error" or bool(self.error)

    @property
    def is_message(self) -> bool:
        return self.type in {"message", "output_text", "output_message"}


def _extract_item_text(item: Any) -> str | None:
    content = _get(item, "content") or []
    if isinstance(content, str):
        return content or None
    chunks: list[str] = []
    for c in content:
        v = _get(c, "text")
        if isinstance(v, str) and v:
            chunks.append(v)
        elif v is not None:
            inner = _get(v, "value")
            if inner:
                chunks.append(inner)
    return "\n".join(chunks) if chunks else None


def parse_activity(raw_response: Any) -> list[ActivityItem]:
    """Normalise ``raw_response['output']`` into :class:`ActivityItem` list."""
    out = _get(raw_response, "output") or []
    items: list[ActivityItem] = []
    for it in out:
        itype = _get(it, "type") or "unknown"
        error = _get(it, "error")
        if isinstance(error, dict):
            error = error.get("message") or str(error)
        items.append(
            ActivityItem(
                type=str(itype),
                id=_get(it, "id"),
                status=_get(it, "status"),
                name=_get(it, "name") or _get(it, "tool_name"),
                server_label=_get(it, "server_label"),
                arguments=_get(it, "arguments"),
                output=_get(it, "output") or _get(it, "outputs"),
                text=_extract_item_text(it),
                error=str(error) if error else None,
                raw=it,
            )
        )
    return items


# ─── Response-level helpers ─────────────────────────────────────────────────


def extract_text(raw_response: Any) -> str | None:
    """Return the assistant's flat text (output_text helper or content walk)."""
    t = _get(raw_response, "output_text")
    if isinstance(t, str) and t:
        return t
    chunks: list[str] = []
    for item in _get(raw_response, "output") or []:
        if _get(item, "type") in {"message", "output_text", "output_message", None}:
            txt = _extract_item_text(item)
            if txt:
                chunks.append(txt)
    return "\n".join(chunks) if chunks else None


def extract_consent_link(items: list[ActivityItem]) -> str | None:
    for it in items:
        if it.is_consent_request:
            link = _get(it.raw, "consent_link") or _get(it.raw, "approval_url")
            if link:
                return link
    return None


def has_code_interpreter_activity(items: list[ActivityItem]) -> bool:
    """True only if a code_interpreter_call item is present (evidence, not text)."""
    return any(it.is_code_interpreter for it in items)


def mcp_tool_calls(
    items: list[ActivityItem], server_label: str = "mcp"
) -> list[ActivityItem]:
    """Function/MCP calls for a given tool, matched by server label or name."""
    needle = server_label.lower()
    matched: list[ActivityItem] = []
    for it in items:
        if not (it.is_function_call or it.is_function_call_output):
            continue
        haystack = " ".join(
            filter(None, [it.server_label, it.name])
        ).lower()
        if needle in haystack:
            matched.append(it)
    return matched


def function_calls(items: list[ActivityItem]) -> list[ActivityItem]:
    return [it for it in items if it.is_function_call]


def errors(items: list[ActivityItem]) -> list[ActivityItem]:
    return [it for it in items if it.is_error]


@dataclass
class ActivitySummary:
    """A compact, UI-friendly summary of one response's tool activity."""

    item_types: list[str]
    has_code_interpreter: bool
    mcp_tool_call_count: int
    function_call_count: int
    consent_link: str | None
    error_messages: list[str]

    def as_dict(self) -> dict:
        return {
            "item_types": self.item_types,
            "has_code_interpreter": self.has_code_interpreter,
            "mcp_tool_call_count": self.mcp_tool_call_count,
            "function_call_count": self.function_call_count,
            "consent_link": self.consent_link,
            "error_messages": self.error_messages,
        }


def summarize(
    items: list[ActivityItem], server_label: str = "mcp"
) -> ActivitySummary:
    return ActivitySummary(
        item_types=[it.type for it in items],
        has_code_interpreter=has_code_interpreter_activity(items),
        mcp_tool_call_count=len(mcp_tool_calls(items, server_label)),
        function_call_count=len(function_calls(items)),
        consent_link=extract_consent_link(items),
        error_messages=[it.error for it in errors(items) if it.error],
    )
