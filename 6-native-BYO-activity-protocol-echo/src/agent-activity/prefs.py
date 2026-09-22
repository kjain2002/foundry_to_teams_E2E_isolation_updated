# Copyright (c) Microsoft. All rights reserved.

"""Default onboarding poll + conversation-history engine (durable).

Applies to ALL Teams publishes routed through this activity agent (not the
underlying agent). On first contact it asks a history-refresh cadence via an
Adaptive Card and supports a RESET** command. State is persisted in Azure Blob
(see state_store) so it survives cold starts, redeploys, and replicas.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from microsoft_agents.activity import Attachment

import state_store

_ADAPTIVE_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

CADENCE_SECONDS = {"hourly": 3600, "daily": 86400, "weekly": 604800, "never": 0}


@dataclass
class Preferences:
    refresh_cadence: str = "never"   # hourly | daily | weekly | never


# Blob prefixes in the state container.
_PREFS = "prefs"   # prefs/{user_id}.json -> {"refresh_cadence": str, "asked": bool}
_HIST = "hist"     # hist/{conversation_id}.json -> {"history": [...], "ts": float}


def has_prefs(user_id: str) -> bool:
    return state_store.get_json(_PREFS, user_id) is not None


def get_prefs(user_id: str) -> Preferences:
    data = state_store.get_json(_PREFS, user_id) or {}
    return Preferences(refresh_cadence=str(data.get("refresh_cadence", "never")))


def set_prefs(user_id: str, prefs: Preferences) -> None:
    state_store.put_json(
        _PREFS, user_id, {"refresh_cadence": prefs.refresh_cadence, "asked": True}
    )


def was_asked(user_id: str) -> bool:
    data = state_store.get_json(_PREFS, user_id) or {}
    return bool(data.get("asked"))


def mark_asked(user_id: str) -> None:
    data = state_store.get_json(_PREFS, user_id) or {}
    data["asked"] = True
    data.setdefault("refresh_cadence", "never")
    state_store.put_json(_PREFS, user_id, data)


def forget_prefs(user_id: str) -> None:
    """Drop saved preferences and the 'asked' flag so the poll re-renders."""
    state_store.delete_json(_PREFS, user_id)


def build_prefs_card() -> Attachment:
    """Adaptive Card with two radio (Input.ChoiceSet) questions + Save."""
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Quick setup", "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": "How regularly would you like conversation history to be refreshed?", "wrap": True},
            {
                "type": "Input.ChoiceSet", "id": "refresh_cadence", "style": "expanded", "value": "never",
                "choices": [
                    {"title": "Hourly", "value": "hourly"},
                    {"title": "Daily", "value": "daily"},
                    {"title": "Weekly", "value": "weekly"},
                    {"title": "Never", "value": "never"},
                ],
            },
            {"type": "TextBlock", "text": "Type RESET** on its own to reset the agent's memory (conversation context + these preferences). It does not clear the visible chat.", "wrap": True, "isSubtle": True, "size": "Small"},
            {"type": "TextBlock", "text": "The Teams 'Remove chat history' button only visually clears the chat and does not reliably reset the agent. To clear both, use that button and type RESET**.", "wrap": True, "isSubtle": True, "size": "Small"},
        ],
        "actions": [{"type": "Action.Submit", "title": "Save", "data": {"kind": "prefs_submit"}}],
    }
    return Attachment(content_type=_ADAPTIVE_CONTENT_TYPE, content=card)


def parse_prefs_submit(activity) -> "Preferences | None":
    """Return Preferences if this activity is the poll's Action.Submit."""
    value = getattr(activity, "value", None)
    if not isinstance(value, dict):
        dump = getattr(value, "model_dump", None)
        value = dump(by_alias=True) if callable(dump) else None
    if not isinstance(value, dict) or value.get("kind") != "prefs_submit":
        return None
    cadence = str(value.get("refresh_cadence") or "never").lower()
    if cadence not in CADENCE_SECONDS:
        cadence = "never"
    return Preferences(refresh_cadence=cadence)


def is_reset_command(text: str) -> bool:
    return (text or "").strip() == "RESET**"


def get_history(conversation_id: str, user_id: str) -> list[dict]:
    """Return history, auto-clearing it if past the user's refresh cadence."""
    entry = state_store.get_json(_HIST, conversation_id)
    if not entry:
        return []
    ttl = CADENCE_SECONDS.get(get_prefs(user_id).refresh_cadence, 0)
    if ttl and (time.time() - entry.get("ts", 0)) > ttl:
        state_store.delete_json(_HIST, conversation_id)
        return []
    return list(entry.get("history", []))


def append_history(conversation_id: str, user_text: str, assistant_text: str) -> None:
    entry = state_store.get_json(_HIST, conversation_id) or {"history": []}
    entry.setdefault("history", []).extend([
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ])
    entry["ts"] = time.time()
    state_store.put_json(_HIST, conversation_id, entry)


def clear_history(conversation_id: str) -> None:
    state_store.delete_json(_HIST, conversation_id)
