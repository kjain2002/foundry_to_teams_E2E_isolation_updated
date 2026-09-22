# Copyright (c) Microsoft. All rights reserved.

"""Parse downloadable file attachments off an incoming Teams activity.

Only the parsing lives here; uploading into the agent's session sandbox is in
sessions.py. (The earlier container-based helpers were removed - src_4 uses the
session $HOME, not a client-created container.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("agent-activity.attachments")

# Teams personal-scope file upload; the real download URL lives in
# attachment.content["downloadUrl"] and is pre-authorized.
TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"

# Rich-content/card attachments that are NOT user files - skip these.
SKIP_CONTENT_TYPES = {
    "application/vnd.microsoft.card.hero",
    "application/vnd.microsoft.card.adaptive",
    "application/vnd.microsoft.card.thumbnail",
    "application/vnd.microsoft.card.signin",
    "text/html",
}

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB


@dataclass
class AttachmentRef:
    name: str
    url: str
    content_type: str | None


def extract_file_attachments(activity) -> list[AttachmentRef]:
    """Pull downloadable file attachments off an activity."""
    refs: list[AttachmentRef] = []
    for att in getattr(activity, "attachments", None) or []:
        ct = getattr(att, "content_type", None) or ""
        name = getattr(att, "name", None) or "file"

        if ct == TEAMS_FILE_DOWNLOAD_INFO:
            content = getattr(att, "content", None) or {}
            url = content.get("downloadUrl") if isinstance(content, dict) else None
            ftype = content.get("fileType") if isinstance(content, dict) else None
            if url:
                if ftype and not name.lower().endswith("." + ftype.lower()):
                    name = f"{name}.{ftype}"
                refs.append(AttachmentRef(name=name, url=url, content_type=ct))
            continue

        if ct in SKIP_CONTENT_TYPES:
            continue

        content_url = getattr(att, "content_url", None)
        if content_url and str(content_url).lower().startswith("http"):
            refs.append(AttachmentRef(name=name, url=content_url, content_type=ct))

    return refs
