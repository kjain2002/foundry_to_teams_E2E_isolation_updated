# Copyright (c) Microsoft. All rights reserved.
"""Send generated files back to the user in Microsoft Teams.

Teams delivers a bot-generated file via the **File Consent** flow:

1. The bot sends a ``FileConsentCard`` attachment (name, description, size).
2. The user clicks *Allow* → Teams sends a ``fileConsent/invoke`` activity that
   carries an ``uploadInfo.uploadUrl`` (a OneDrive upload session URL).
3. The bot ``PUT``s the raw bytes to that URL, then sends a ``FileInfoCard`` so
   the file renders as a downloadable attachment in the chat.

Note: this rich card flow is a **Teams personal-scope** feature. It does not
render in M365 Copilot (BizChat), which strips/omits file cards the same way it
omits inbound file attachments.
"""

from __future__ import annotations

import logging
import os
import uuid

logger = logging.getLogger("github-copilot.outfiles")

_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
_INVOKE_NAME = "fileConsent/invoke"

# Bytes are stashed here (durable per-session $HOME) keyed by a token so the
# accept invoke can recover them. Base64-embedding the file in the card's
# acceptContext overflows Teams' card size limit for large files (e.g. .pptx),
# which makes the whole consent card silently fail to render.
_CACHE_DIR = os.path.join(os.environ.get("HOME") or ".", ".github-copilot", "outfiles")
# Only fall back to embedding bytes in the card when the cache write fails AND
# the file is tiny enough to keep the card under Teams' limit (~pre-b64 size).
_EMBED_FALLBACK_MAX = 16 * 1024


def build_file_consent(filename: str, data: bytes) -> tuple[object, str]:
    """Build a FileConsentCard; stash bytes in $HOME keyed by a token.

    The accept invoke is a separate request, so the bytes must survive the turn.
    We write them to a durable per-session cache and carry only a small token in
    ``acceptContext`` — embedding the whole file (base64) overflows the Teams
    card size limit for large files and the card never renders.

    Returns ``(attachment, lead_text)``.
    """
    from microsoft_agents.activity import Attachment

    ext = filename.rsplit(".", 1)[-1] if "." in filename else "txt"
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    token = uuid.uuid4().hex
    accept: dict[str, object] = {"filename": filename, "fileType": ext, "token": token}
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_CACHE_DIR, token), "wb") as fh:
            fh.write(raw)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Cache unavailable: embed inline only if small enough to keep the card valid.
        logger.warning("outfiles cache write failed (%s); embedding inline.", exc)
        if len(raw) <= _EMBED_FALLBACK_MAX:
            import base64
            accept["b64"] = base64.b64encode(raw).decode("ascii")
    consent = {
        "description": f"Generated document: {filename}",
        "sizeInBytes": len(raw),
        "acceptContext": accept,
        "declineContext": {"filename": filename},
    }
    attachment = Attachment(
        content_type=_CONSENT_CONTENT_TYPE,
        name=filename,
        content=consent,
    )
    lead = f"I've prepared **{filename}**. Click *Allow* to download it here."
    return attachment, lead


def is_file_consent_invoke(activity) -> bool:
    return (getattr(activity, "type", None) == "invoke"
            and getattr(activity, "name", None) == _INVOKE_NAME)


async def handle_file_consent_invoke(context) -> None:
    """Process a fileConsent/invoke: upload the content bytes on accept."""
    import base64

    activity = context.activity
    value = getattr(activity, "value", None) or {}
    if not isinstance(value, dict):
        # pydantic model → dict
        dump = getattr(value, "model_dump", None)
        value = dump(by_alias=True) if callable(dump) else {}

    action = value.get("action")
    ctx = value.get("context") or {}
    upload = value.get("uploadInfo") or {}

    if action == "decline":
        await context.send_activity("No problem — I won't send the file.")
        return

    if action != "accept":
        return

    filename = (ctx or {}).get("filename") or (upload.get("name") if isinstance(upload, dict) else None) or "file.txt"
    upload_url = upload.get("uploadUrl") if isinstance(upload, dict) else None
    unique_id = upload.get("uniqueId") if isinstance(upload, dict) else None
    file_type = (ctx or {}).get("fileType") or "txt"

    # Recover the file bytes: prefer the $HOME cache (token), fall back to inline b64.
    data = b""
    token = (ctx or {}).get("token")
    if token:
        cache_path = os.path.join(_CACHE_DIR, os.path.basename(str(token)))
        try:
            with open(cache_path, "rb") as fh:
                data = fh.read()
            os.remove(cache_path)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("outfiles cache read failed for %s: %s", token, exc)
    if not data:
        b64 = (ctx or {}).get("b64")
        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("b64 decode failed: %s", exc)
                data = b""

    if not (upload_url and data):
        logger.warning("CONSENT_INVOKE | cannot upload (url=%s bytes=%d)", bool(upload_url), len(data))
        await context.send_activity("Sorry — I couldn't complete the file upload.")
        return

    # PUT the bytes to the OneDrive upload session.
    import httpx

    size = len(data)
    headers = {
        "Content-Length": str(size),
        "Content-Range": f"bytes 0-{size - 1}/{size}",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.put(upload_url, content=data, headers=headers)
            resp.raise_for_status()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("CONSENT_UPLOAD | PUT failed: %s", exc, exc_info=True)
        await context.send_activity("Sorry — the file upload failed.")
        return

    # Send a FileInfoCard so the file renders as a downloadable attachment.
    from microsoft_agents.activity import Attachment

    info = {"uniqueId": unique_id, "fileType": file_type}
    content_url = upload.get("contentUrl") if isinstance(upload, dict) else None
    info_att = Attachment(
        content_type=_INFO_CONTENT_TYPE,
        name=filename,
        content_url=content_url,
        content=info,
    )
    try:
        from microsoft_agents.activity import Activity, ActivityTypes
        await context.send_activity(Activity(type=ActivityTypes.message, attachments=[info_att]))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("send FileInfoCard failed: %s", exc, exc_info=True)
