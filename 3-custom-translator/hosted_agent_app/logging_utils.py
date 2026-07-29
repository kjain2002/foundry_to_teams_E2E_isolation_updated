"""Structured per-turn diagnostics for the hosted-agent app.

Emits one structured record per turn. Secrets are never included: bearer
tokens, client secrets, Authorization headers and full consent URLs (which can
carry sensitive state) are redacted or dropped.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("hosted_agent_app.turn")

# Patterns we scrub from any free-form string before it is logged/persisted.
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._\-]+")
_SECRET_KEYS = re.compile(r"(?i)(authorization|token|secret|password|api[-_]?key)")


def redact(value: Any) -> Any:
    """Recursively redact secret-looking content from a value."""
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer [REDACTED]", value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def redact_consent_link(link: str | None) -> str | None:
    """Keep only the origin+path of a consent URL; drop the query (state/tokens)."""
    if not link:
        return None
    return link.split("?", 1)[0] + ("?[REDACTED]" if "?" in link else "")


@dataclass
class TurnDiagnostics:
    """One structured turn record (safe to log / download as JSON)."""

    conversation_id: str
    session_id: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    request_id: str | None = None
    http_status: int | None = None
    response_status: str | None = None
    response_id: str | None = None
    activity_item_types: list[str] = field(default_factory=list)
    activity_item_ids: list[str] = field(default_factory=list)
    container_id: str | None = None
    has_code_interpreter: bool = False
    mcp_tool_call_count: int = 0
    generated_file_names: list[str] = field(default_factory=list)
    generated_file_ids: list[str] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # Defensive: scrub any accidental secret content in free-form fields.
        if d.get("error"):
            d["error"] = redact(d["error"])
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def emit(self) -> None:
        logger.info("turn %s", json.dumps(self.to_dict(), default=str))
