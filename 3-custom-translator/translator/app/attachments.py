"""Teams attachment ingestion for the translator.

Reads file attachments off an incoming Bot Framework Activity, downloads the
bytes, and uploads them into a client-created Foundry Code Interpreter
container so the hosted agent's Code Interpreter can access them.

This is the supported private-network (PN) workaround: create a container, put
the files in it, and pass the ``container_id`` to Code Interpreter (portal file
upload/download isn't supported in PN — see the Foundry private-link docs).

Design goals:
    * accept a BROAD range of file types (no extension allow-list — Code
      Interpreter accepts many formats; we only cap size);
    * accept MULTIPLE files in a single message;
    * never fabricate success — a file that can't be downloaded/uploaded is
      reported as an error, not silently dropped.

No secrets are logged. The Foundry bearer token is fetched per call from the
translator's managed identity credential and never persisted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("translator.attachments")

FOUNDRY_SCOPE = "https://ai.azure.com/.default"

# Teams personal-scope file upload arrives with this content type; the real
# download URL lives in attachment.content["downloadUrl"] and is pre-authorized.
TEAMS_FILE_DOWNLOAD_INFO = "application/vnd.microsoft.teams.file.download.info"

# Rich-content/card attachments that are NOT user files — skip these.
SKIP_CONTENT_TYPES = {
    "application/vnd.microsoft.card.hero",
    "application/vnd.microsoft.card.adaptive",
    "application/vnd.microsoft.card.thumbnail",
    "application/vnd.microsoft.card.signin",
    "text/html",
}

# Generous per-file cap so we accept almost anything a user attaches.
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB


@dataclass
class AttachmentRef:
    name: str
    url: str
    content_type: str | None


@dataclass
class UploadedFile:
    filename: str
    file_id: str
    container_id: str
    size: int
    path: str | None = None


@dataclass
class IngestResult:
    container_id: str | None
    uploaded: list[UploadedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def extract_file_attachments(activity) -> list[AttachmentRef]:
    """Pull downloadable file attachments off a Bot Framework Activity."""
    refs: list[AttachmentRef] = []
    for att in (getattr(activity, "attachments", None) or []):
        ct = (getattr(att, "content_type", None) or "")
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

        # Generic inline attachment (images, files referenced by contentUrl).
        content_url = getattr(att, "content_url", None)
        if content_url and str(content_url).lower().startswith("http"):
            refs.append(AttachmentRef(name=name, url=content_url, content_type=ct))

    return refs


class ContainerUploader:
    """Minimal async REST client for the project-scoped containers surface."""

    def __init__(self, project_endpoint: str, credential, api_version: str) -> None:
        self._base = project_endpoint.rstrip("/") + "/openai/v1/containers"
        self._cred = credential
        self._api = api_version

    async def _headers(self) -> dict:
        token = await self._cred.get_token(FOUNDRY_SCOPE)
        return {"Authorization": f"Bearer {token.token}"}

    async def ensure_container(
        self, session: aiohttp.ClientSession, existing: str | None,
        name: str = "teams-upload",
    ) -> str:
        if existing:
            return existing
        async with session.post(
            self._base,
            headers=await self._headers(),
            json={"name": name},
        ) as r:
            if r.status >= 400:
                raise RuntimeError(f"create_container [{r.status}]: {(await r.text())[:300]}")
            data = await r.json()
            cid = data.get("id")
            if not cid:
                raise RuntimeError(f"create_container returned no id: {data}")
            log.info("created container %s for Teams upload", cid)
            return cid

    async def upload_file(
        self, session: aiohttp.ClientSession, container_id: str,
        filename: str, content: bytes,
    ) -> dict:
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename)
        url = f"{self._base}/{container_id}/files"
        async with session.post(
            url,
            headers=await self._headers(),
            data=form,
        ) as r:
            if r.status >= 400:
                raise RuntimeError(f"upload_file [{r.status}]: {(await r.text())[:300]}")
            return await r.json()


async def create_container(
    credential, project_endpoint: str, api_version: str, name: str = "teams-ci"
) -> str:
    """Create a fresh Code Interpreter container and return its id."""
    uploader = ContainerUploader(project_endpoint, credential, api_version)
    async with aiohttp.ClientSession() as session:
        return await uploader.ensure_container(session, None, name)


async def _download(session: aiohttp.ClientSession, ref: AttachmentRef) -> bytes:
    async with session.get(ref.url) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"download [{resp.status}]")
        return await resp.read()


async def ingest_activity_attachments(
    activity,
    credential,
    project_endpoint: str,
    api_version: str,
    container_id: str | None = None,
) -> IngestResult:
    """Download every file attachment and upload it into a Foundry container.

    Returns an :class:`IngestResult`. Partial success is possible: some files
    may upload while others report errors. Never fakes a successful upload.
    """
    refs = extract_file_attachments(activity)
    result = IngestResult(container_id=container_id)
    if not refs:
        return result

    uploader = ContainerUploader(project_endpoint, credential, api_version)
    async with aiohttp.ClientSession() as session:
        for ref in refs:
            try:
                data = await _download(session, ref)
                if len(data) > MAX_FILE_BYTES:
                    result.errors.append(
                        f"{ref.name}: too large ({len(data)} bytes > {MAX_FILE_BYTES})"
                    )
                    continue
                result.container_id = await uploader.ensure_container(
                    session, result.container_id
                )
                info = await uploader.upload_file(
                    session, result.container_id, ref.name, data
                )
                result.uploaded.append(
                    UploadedFile(
                        filename=ref.name,
                        file_id=info.get("id", ""),
                        container_id=result.container_id,
                        size=len(data),
                        path=info.get("path") or f"/mnt/data/{ref.name}",
                    )
                )
                log.info("uploaded %s (%d bytes) to %s", ref.name, len(data), result.container_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("attachment ingest failed for %s", ref.name)
                result.errors.append(f"{ref.name}: {exc}")

    return result



# ─── Outbound: return generated container files to Teams ────────────────────


async def list_container_files(
    credential, project_endpoint: str, container_id: str
) -> list[dict]:
    """List files in a container (id/path/source), for assistant-file detection."""
    base = project_endpoint.rstrip("/") + "/openai/v1/containers"
    token = await credential.get_token(FOUNDRY_SCOPE)
    url = f"{base}/{container_id}/files"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers={"Authorization": f"Bearer {token.token}"}
        ) as r:
            if r.status >= 400:
                raise RuntimeError(
                    f"list_container_files [{r.status}]: {(await r.text())[:200]}"
                )
            body = await r.json()
            return body.get("data", body) if isinstance(body, dict) else body


async def download_container_file(
    credential, project_endpoint: str, container_id: str, file_id: str
) -> bytes:
    """Download the bytes of a file the agent generated inside a container."""
    base = project_endpoint.rstrip("/") + "/openai/v1/containers"
    token = await credential.get_token(FOUNDRY_SCOPE)
    url = f"{base}/{container_id}/files/{file_id}/content"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, headers={"Authorization": f"Bearer {token.token}"}
        ) as r:
            if r.status >= 400:
                raise RuntimeError(
                    f"download_container_file [{r.status}]: {(await r.text())[:200]}"
                )
            return await r.read()


async def put_file_to_teams(upload_url: str, content: bytes) -> None:
    """PUT file bytes to the Teams/OneDrive upload URL from a file-consent accept.

    The ``uploadUrl`` returned by Teams is pre-authorized; a single ranged PUT
    works for the modest deck sizes we produce.
    """
    headers = {
        "Content-Length": str(len(content)),
        "Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(upload_url, data=content, headers=headers) as r:
            if r.status >= 300:
                raise RuntimeError(
                    f"teams upload [{r.status}]: {(await r.text())[:200]}"
                )

