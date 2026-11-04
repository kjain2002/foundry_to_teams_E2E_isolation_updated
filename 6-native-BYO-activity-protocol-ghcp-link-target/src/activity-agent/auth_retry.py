"""Short-lived, user/session-bound state for resuming a consent-blocked tool."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("github-copilot.auth-retry")
_STATE_DIR = Path(os.environ.get("HOME") or ".") / ".github-copilot" / "pending-auth"
_TTL_SECONDS = 20 * 60
_ACTIVE: dict[str, dict[str, str]] = {}


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path(conversation_id: str, user_id: str) -> Path:
    return _STATE_DIR / _key(conversation_id) / (_key(user_id) + ".json")


def begin_turn(conversation_id: str, user_id: str, request: str, nonce: str) -> None:
    _ACTIVE[conversation_id] = {"user": user_id, "request": request, "nonce": nonce}


def end_turn(conversation_id: str) -> None:
    _ACTIVE.pop(conversation_id, None)


def _contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower().replace("-", "_")
            if any(part in name for part in ("secret", "password", "authorization", "access_token", "refresh_token", "api_key", "apikey")):
                return True
            if _contains_credentials(item):
                return True
    elif isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    return False


def _pending(conversation_id: str, user_id: str, nonce: str) -> list[tuple[Path, dict[str, Any]]]:
    if not user_id:
        return []
    legacy = _path(conversation_id, user_id)
    paths = [legacy, *legacy.parent.glob(legacy.stem + "-*.json")]
    pending = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid pending state.")
            age = time.time() - float(data["created_at"])
            if (data.get("user") != user_id or data.get("nonce") != nonce or
                    not 0 <= age < _TTL_SECONDS or not isinstance(data.get("operation"), dict) or
                    not isinstance(data.get("request"), str) or not isinstance(data.get("id"), str)):
                path.unlink(missing_ok=True)
                continue
            pending.append((path, data))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(pending, key=lambda item: (float(item[1]["created_at"]), item[0].name))


def peek(conversation_id: str, user_id: str, nonce: str) -> dict[str, Any] | None:
    pending = _pending(conversation_id, user_id, nonce)
    return pending[0][1] if pending else None


def stage(conversation_id: str, operation: dict[str, Any]) -> str | None:
    active = _ACTIVE.get(conversation_id)
    if not active or not active["user"] or not active["request"] or _contains_credentials(operation):
        return None
    for _path_value, current in _pending(conversation_id, active["user"], active["nonce"]):
        if current["operation"] == operation and current["request"] == active["request"]:
            return current["id"]
    data = {**active, "id": uuid.uuid4().hex, "created_at": time.time(), "operation": operation}
    encoded = json.dumps(data, ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 65536:
        return None
    legacy = _path(conversation_id, active["user"])
    path = legacy.with_name(legacy.stem + "-" + data["id"] + ".json")
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)
        return data["id"]
    except OSError:
        logger.warning("Could not save pending authorization retry state.")
        return None
    finally:
        temporary.unlink(missing_ok=True)


def _restore_claim(claimed: Path, path: Path) -> None:
    try:
        os.link(claimed, path)
    except OSError:
        logger.warning("Could not restore pending authorization state; claim retained for recovery.")
        return
    try:
        claimed.unlink(missing_ok=True)
    except OSError:
        logger.warning("Pending authorization state restored but claim cleanup failed.")


def take(conversation_id: str, user_id: str, nonce: str, retry_id: str | None = None) -> dict[str, Any] | None:
    pending = _pending(conversation_id, user_id, nonce)
    selected = next(((path, data) for path, data in pending if retry_id is None or data["id"] == retry_id), None)
    if selected is None:
        return None
    path, data = selected
    claimed = path.with_suffix("." + uuid.uuid4().hex + ".claimed")
    renamed = False
    consumed = False
    try:
        path.rename(claimed)
        renamed = True
        claimed_data = json.loads(claimed.read_text(encoding="utf-8"))
        if claimed_data != data:
            _restore_claim(claimed, path)
            return None
        consumed = True
        return data
    except OSError:
        if renamed:
            _restore_claim(claimed, path)
        return None
    except ValueError:
        logger.warning("Invalid pending authorization claim retained for recovery.")
        return None
    finally:
        if consumed:
            try:
                claimed.unlink(missing_ok=True)
            except OSError:
                logger.warning("Consumed authorization claim cleanup failed; claim will not be replayed.")


def clear_conversation(conversation_id: str) -> None:
    end_turn(conversation_id)
    directory = _STATE_DIR / _key(conversation_id)
    if directory.is_dir():
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)