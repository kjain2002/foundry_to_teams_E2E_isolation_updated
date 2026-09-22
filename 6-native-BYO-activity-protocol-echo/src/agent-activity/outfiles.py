# Copyright (c) Microsoft. All rights reserved.

"""Send generated files back to Teams via the File Consent flow.

Ported from the github-copilot PG sample (same SDK: microsoft_agents.activity),
but adapted to **re-fetch the bytes from the agent's session sandbox on Accept**
instead of base64-embedding them in the consent card. Embedding a large file
(e.g. a 14 MB pptx -> ~19 MB base64) would exceed the card/activity size limit;
re-fetching keeps the card tiny.

Teams delivers a bot-generated file via the File Consent flow:
1. The bot sends a FileConsentCard (name, description, size).
2. The user clicks Allow -> Teams sends a ``fileConsent/invoke`` carrying an
   ``uploadInfo.uploadUrl`` (a OneDrive upload session URL).
3. The bot PUTs the bytes to that URL, then sends a FileInfoCard so the file
   renders as a downloadable attachment.

This rich card flow is a Teams personal-scope feature; it doesn't render in
M365 Copilot (which omits file cards).
"""

from __future__ import annotations

import logging

import httpx
from microsoft_agents.activity import Activity, ActivityTypes, Attachment

logger = logging.getLogger("agent-activity.outfiles")

_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"
_INVOKE_NAME = "fileConsent/invoke"


def build_file_consent(filename: str, session_id: str, size: int) -> tuple[Attachment, str]:
    """Build a FileConsentCard; bytes are re-fetched from the session on Accept."""
    consent = {
        "description": f"Generated file: {filename}",
        "sizeInBytes": size,
        "acceptContext": {"filename": filename, "session_id": session_id},
        "declineContext": {"filename": filename},
    }
    att = Attachment(content_type=_CONSENT_CONTENT_TYPE, name=filename, content=consent)
    lead = f"I've prepared **{filename}**. Click *Allow* to download it here."
    return att, lead


def is_file_consent_invoke(activity) -> bool:
    return (
        getattr(activity, "type", None) == "invoke"
        and getattr(activity, "name", None) == _INVOKE_NAME
    )


async def handle_file_consent_invoke(context, fetch_bytes) -> None:
    """On Accept, re-fetch the bytes and PUT them to the OneDrive upload URL.

    ``fetch_bytes`` is ``async (session_id, filename) -> bytes``.
    """
    activity = context.activity
    value = getattr(activity, "value", None) or {}
    if not isinstance(value, dict):
        dump = getattr(value, "model_dump", None)
        value = dump(by_alias=True) if callable(dump) else {}

    action = value.get("action")
    ctx = value.get("context") or {}
    upload = value.get("uploadInfo") or {}

    if action == "decline":
        await context.send_activity("No problem - I won't send the file.")
        return
    if action != "accept":
        return

    filename = (ctx or {}).get("filename") or "file"
    session_id = (ctx or {}).get("session_id")
    upload_url = upload.get("uploadUrl") if isinstance(upload, dict) else None
    unique_id = upload.get("uniqueId") if isinstance(upload, dict) else None
    content_url = upload.get("contentUrl") if isinstance(upload, dict) else None

    data = b""
    if session_id:
        try:
            data = await fetch_bytes(session_id, filename)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("re-fetch of %s failed: %s", filename, exc)

    if not (upload_url and data):
        await context.send_activity("Sorry - I couldn't complete the file upload.")
        return

    size = len(data)
    headers = {
        "Content-Length": str(size),
        "Content-Range": f"bytes 0-{size - 1}/{size}",
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as http:
            resp = await http.put(upload_url, content=data, headers=headers)
            resp.raise_for_status()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("consent PUT failed: %s", exc, exc_info=True)
        await context.send_activity("Sorry - the file upload failed.")
        return

    ext = filename.rsplit(".", 1)[-1] if "." in filename else "txt"
    info = {"uniqueId": unique_id, "fileType": ext}
    info_att = Attachment(
        content_type=_INFO_CONTENT_TYPE,
        name=filename,
        content_url=content_url,
        content=info,
    )
    try:
        await context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[info_att])
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("send FileInfoCard failed: %s", exc, exc_info=True)
