# Copyright (c) Microsoft. All rights reserved.
"""Download files the user shares in Teams and hand them to the Copilot SDK.

When a user attaches a file, Teams sends a ``message`` activity with an
attachment of type ``application/vnd.microsoft.teams.file.download.info`` whose
``content.downloadUrl`` is a pre-authenticated URL. We download the **raw
bytes** to a per-conversation folder under ``$HOME`` and hand the file *path*
straight to the model as an attachment — we do **no** parsing/extraction, so any
file type (PDF, PPTX, images, spreadsheets, code, …) is the model's to read and
reason over using its own tools.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("github-copilot.files")

# Raw shared files land here (one folder per conversation) so the model can open
# them by path via its file tools.
_HOME = os.environ.get("HOME") or "."
_FILES_DIR = Path(_HOME) / ".github-copilot" / "files"
_ATTACHMENT_DEBUG = (os.environ.get("ATTACHMENT_DEBUG", "0") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_ATTACHMENT_DEBUG_DIR = Path(
    os.environ.get(
        "ATTACHMENT_DEBUG_DIR",
        str(Path(_HOME) / ".github-copilot" / "attachment-debug"),
    )
)
_ATTACHMENT_DEBUG_BODY_CHARS = int(os.environ.get("ATTACHMENT_DEBUG_BODY_CHARS", "800"))

# A progress sink: short human-readable status lines while a file downloads.
Progress = Optional[Callable[[str], None]]


def _emit(on_progress: Progress, text: str) -> None:
    if on_progress is not None:
        try:
            on_progress(text)
        except Exception:  # pylint: disable=broad-exception-caught
            pass


def _conv_dir(conversation_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id) or "default"
    return _FILES_DIR / safe


def _safe_model_dump(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(by_alias=True)
        except Exception:  # pylint: disable=broad-exception-caught
            return str(value)
    return str(value)


def _redact_key_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    low = key.lower()
    if "token" in low or "authorization" in low:
        return "<redacted>"
    if "url" in low and "?" in value:
        return value.split("?", 1)[0] + "?<redacted_query>"
    return value


def _sanitize_payload(value: Any, parent_key: str = "") -> Any:
    value = _safe_model_dump(value)
    if isinstance(value, dict):
        return {
            str(k): _sanitize_payload(_redact_key_value(str(k), v), str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_payload(v, parent_key) for v in value]
    if isinstance(value, str) and len(value) > _ATTACHMENT_DEBUG_BODY_CHARS:
        return value[:_ATTACHMENT_DEBUG_BODY_CHARS] + "...<truncated>"
    return value


def _attachment_snapshot(att: Any, index: int) -> dict[str, Any]:
    content = _safe_model_dump(getattr(att, "content", None))
    content_kind = type(content).__name__ if content is not None else "None"
    content_keys = sorted(content.keys()) if isinstance(content, dict) else []
    snapshot: dict[str, Any] = {
        "index": index,
        "name": getattr(att, "name", None),
        "content_type": getattr(att, "content_type", None),
        "content_url": getattr(att, "content_url", None),
        "content_kind": content_kind,
        "content_keys": content_keys,
        "has_download_url": bool(isinstance(content, dict) and content.get("downloadUrl")),
    }
    if content is not None:
        snapshot["content"] = _sanitize_payload(content)
    return snapshot


def describe_attachments(activity: Any) -> str:
    """Human-readable attachment summary for quick Teams debugging."""
    items = []
    for idx, att in enumerate(getattr(activity, "attachments", None) or []):
        snap = _attachment_snapshot(att, idx)
        items.append(
            f"[{idx}] type={snap.get('content_type')!r} name={snap.get('name')!r} "
            f"downloadUrl={snap.get('has_download_url')} keys={snap.get('content_keys')} "
            f"content_url={snap.get('content_url')!r}"
        )
    if not items:
        return "No attachments found on this activity."
    return "Attachment summary:\n" + "\n".join(items)


def _write_debug_snapshot(conversation_id: str, payload: dict[str, Any]) -> None:
    if not _ATTACHMENT_DEBUG:
        return
    try:
        _ATTACHMENT_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        safe_conv = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id) or "default"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        out = _ATTACHMENT_DEBUG_DIR / f"{safe_conv}_{ts}.json"
        out.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        logger.info("ATTACHMENT_DEBUG wrote snapshot file=%s", out)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.warning("ATTACHMENT_DEBUG failed writing snapshot: %s", ex)


async def download_shared_files(
    activity: Any,
    conversation_id: str,
    on_progress: Progress = None,
    connector: Any = None,
) -> list[dict[str, str]]:
    """Download each shared file/image verbatim and return item dicts.

    Each item is ``{name, path, kind}`` where ``kind`` is ``"file"`` or
    ``"image"``; image items also carry a ``mime`` field. No extraction: raw
    bytes are written to disk and handed to the model (files by path, images as
    inline base64 for vision).

    Teams file uploads arrive as ``file.download.info`` with a pre-authenticated
    ``downloadUrl``. M365 Copilot images arrive as ``image/*`` with a Bot
    Connector ``content_url`` (``.../v3/attachments/{id}/views/{view}``) that
    requires the authenticated ``connector`` (from the turn context) to fetch.
    """
    import httpx

    out: list[dict[str, str]] = []
    attachments = list(getattr(activity, "attachments", None) or [])
    summary = [
        {
            "index": i,
            "content_type": getattr(att, "content_type", None),
            "name": getattr(att, "name", None),
            "has_content_url": bool(getattr(att, "content_url", None)),
            "has_download_url": bool(_download_url(att)),
        }
        for i, att in enumerate(attachments)
    ]
    logger.info(
        "ATTACHMENT_SCAN conversation_id=%s attachment_count=%d summary=%s",
        conversation_id,
        len(attachments),
        summary,
    )
    _write_debug_snapshot(
        conversation_id,
        {
            "conversation_id": conversation_id,
            "attachment_count": len(attachments),
            "attachments": [_attachment_snapshot(att, i) for i, att in enumerate(attachments)],
        },
    )

    dest = _conv_dir(conversation_id)
    dest.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for att in attachments:
            # Any attachment that carries a pre-authenticated download URL is a
            # shared file — download it verbatim (Teams paperclip uploads).
            url = _download_url(att)
            if url:
                name = getattr(att, "name", None) or "attachment"
                try:
                    _emit(on_progress, f"Downloading {name}…")
                    resp = await client.get(url)
                    resp.raise_for_status()
                    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"
                    path = dest / safe_name
                    path.write_bytes(resp.content)
                    out.append({"name": name, "path": str(path), "kind": "file"})
                except Exception as ex:  # pylint: disable=broad-exception-caught
                    logger.warning("Could not download %s: %s", name, ex)
                continue

            # An image comes as ``image/*`` with a Bot Connector content_url that
            # needs the authenticated connector to fetch (Teams + Copilot).
            if (getattr(att, "content_type", "") or "").lower().startswith("image/"):
                img = await _download_connector_image(att, dest, on_progress, connector)
                if img:
                    out.append(img)
                continue

            logger.info(
                "ATTACHMENT_SKIPPED content_type=%r name=%r keys=%s",
                getattr(att, "content_type", None),
                getattr(att, "name", None),
                _attachment_snapshot(att, -1).get("content_keys"),
            )
    return out


def _parse_attachment_ref(url: str) -> tuple[str, str] | None:
    """Extract (attachment_id, view_id) from a Bot Connector attachment URL."""
    m = re.search(r"/v3/attachments/([^/]+)/views/([^/?]+)", url or "")
    return (m.group(1), m.group(2)) if m else None


async def _download_connector_image(
    att: Any, dest: Path, on_progress: Progress, connector: Any
) -> dict[str, str] | None:
    """Fetch an inline image via the authenticated turn connector client."""
    url = getattr(att, "content_url", None) or ""
    ref = _parse_attachment_ref(url)
    if not ref:
        logger.warning("IMAGE | unrecognized content_url=%r", url)
        return None
    if connector is None:
        logger.warning("IMAGE | no connector on turn; cannot fetch %s", url)
        _emit(on_progress, "Couldn't read the image (no channel connector).")
        return None
    attachment_id, view_id = ref
    try:
        _emit(on_progress, "Reading the image…")
        bio = await connector.attachments.get_attachment(attachment_id, view_id)
        data = bio.getvalue() if hasattr(bio, "getvalue") else bytes(bio)
        # The attachment already declares its type (this branch only runs for
        # ``image/*``); use it directly for the vision blob's mimeType.
        mime = (getattr(att, "content_type", None) or "image/png").lower()
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".img")
        name = getattr(att, "name", None) or f"image{ext}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or f"image{ext}"
        path = dest / safe_name
        path.write_bytes(data)
        return {"name": name, "path": str(path), "kind": "image", "mime": mime}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        logger.warning("Could not fetch image %s: %s", attachment_id, ex)
        _emit(on_progress, "Couldn't read the image.")
        return None


def _download_url(att: Any) -> str | None:
    """Return the pre-authenticated download URL for a Teams file attachment."""
    content = getattr(att, "content", None)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None
    elif content is not None and not isinstance(content, dict):
        dump = getattr(content, "model_dump", None)
        content = dump(by_alias=True) if callable(dump) else None
    return content.get("downloadUrl") if isinstance(content, dict) else None

