# Copyright (c) Microsoft. All rights reserved.

"""Session-sandbox ($HOME) file API for the ci_files agent (matches ui_4/src_4).

The deployed agent reads uploaded inputs and writes generated outputs in its
session sandbox ($HOME) - the same place the playground "Files" panel shows.
There's no Files panel in Teams, so the bot becomes a programmatic client of the
same session-files API: create a session, PUT uploaded files into it, bind the
turn to it via ``agent_session_id`` on the Responses call, and GET generated
files back out. This matches src_4 exactly (NOT the container/USE_CONTAINER_ID
approach, which src_4 ignores).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from attachments import MAX_FILE_BYTES, extract_file_attachments

logger = logging.getLogger("agent-activity.sessions")

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
API_VERSION = "v1"


@dataclass
class UploadedFile:
    filename: str
    path: str


@dataclass
class IngestResult:
    session_id: str | None = None
    uploaded: list[UploadedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _sessions_url(project_endpoint: str, agent_name: str) -> str:
    base = project_endpoint.rstrip("/")
    return f"{base}/agents/{agent_name}/endpoint/sessions"


async def _token(cred) -> str:
    return (await cred.get_token(FOUNDRY_SCOPE)).token


async def ensure_session(
    cred, project_endpoint: str, agent_name: str, existing: str | None = None
) -> str:
    """Create (or reuse) a hosted-agent session; return its id once Active."""
    if existing:
        return existing
    url = _sessions_url(project_endpoint, agent_name)
    token = await _token(cred)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(
            f"{url}?api-version={API_VERSION}",
            headers={**headers, "Content-Type": "application/json"},
            json={},
        )
        r.raise_for_status()
        data = r.json()
        sid = data.get("agent_session_id") or data.get("id")
        # Poll until the sandbox is Active so uploads/turns don't race startup.
        for _ in range(30):
            g = await c.get(f"{url}/{sid}?api-version={API_VERSION}", headers=headers)
            status = str((g.json() or {}).get("status", "")).lower() if g.status_code == 200 else ""
            if status in ("active", "ready", "running"):
                break
            if status == "failed":
                raise RuntimeError(f"session {sid} failed to start")
            await asyncio.sleep(1)
    logger.info("session %s ready", sid)
    return sid


async def list_session_files(
    cred, project_endpoint: str, agent_name: str, session_id: str
) -> set[str]:
    """Names of non-directory files currently in the session sandbox."""
    url = f"{_sessions_url(project_endpoint, agent_name)}/{session_id}/files?api-version={API_VERSION}&path=."
    token = await _token(cred)
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                return set()
            data = r.json()
            entries = (
                data.get("entries")
                or data.get("value")
                or data.get("data")
                or (data if isinstance(data, list) else [])
            )
            return {
                e.get("name")
                for e in entries
                if e.get("name") and not e.get("is_directory")
            }
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("list_session_files failed", exc_info=True)
        return set()


async def upload_session_file(
    cred, project_endpoint: str, agent_name: str, session_id: str,
    filename: str, content: bytes,
) -> None:
    """PUT bytes into the session sandbox ($HOME) via the session files API."""
    url = (
        f"{_sessions_url(project_endpoint, agent_name)}/{session_id}"
        f"/files/content?api-version={API_VERSION}&path={quote(filename)}"
    )
    token = await _token(cred)
    async with httpx.AsyncClient(timeout=180.0) as c:
        r = await c.put(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=content,
        )
        r.raise_for_status()


async def download_session_file(
    cred, project_endpoint: str, agent_name: str, session_id: str, name: str
) -> bytes:
    """GET the bytes of a file from the session sandbox."""
    url = (
        f"{_sessions_url(project_endpoint, agent_name)}/{session_id}"
        f"/files/content?api-version={API_VERSION}&path={quote(name)}"
    )
    token = await _token(cred)
    async with httpx.AsyncClient(timeout=180.0) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content


async def ingest_activity_attachments(
    activity, cred, project_endpoint: str, agent_name: str,
    session_id: str | None = None,
) -> IngestResult:
    """Download Teams file attachments and upload them into the session sandbox."""
    refs = extract_file_attachments(activity)
    result = IngestResult(session_id=session_id)
    if not refs:
        return result

    result.session_id = await ensure_session(cred, project_endpoint, agent_name, session_id)
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as c:
        for ref in refs:
            try:
                dl = await c.get(ref.url)
                dl.raise_for_status()
                data = dl.content
                if len(data) > MAX_FILE_BYTES:
                    result.errors.append(f"{ref.name}: too large ({len(data)} bytes)")
                    continue
                await upload_session_file(
                    cred, project_endpoint, agent_name, result.session_id, ref.name, data
                )
                result.uploaded.append(
                    UploadedFile(filename=ref.name, path=f"/mnt/data/{ref.name}")
                )
                logger.info("uploaded %s (%d bytes) to session %s", ref.name, len(data), result.session_id)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.exception("attachment ingest failed for %s", ref.name)
                result.errors.append(f"{ref.name}: {exc}")

    return result
