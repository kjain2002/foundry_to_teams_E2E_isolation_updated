"""Client-created Foundry Code Interpreter container management.

Identity model (important):
    * The **client / middleware** creates the container using the *caller's*
      identity — the token passed here.
    * The hosted-agent managed identity later *uses* the supplied container id
      (via the ``USE_CONTAINER_ID`` hint / bound tool) but does NOT create it.

REST surface (project-scoped OpenAI v1):
    POST   {project}/openai/v1/containers
    GET    {project}/openai/v1/containers/{id}/files
    POST   {project}/openai/v1/containers/{id}/files          (multipart upload)
    GET    {project}/openai/v1/containers/{id}/files/{fid}/content

No secrets are logged. The bearer token is supplied per call by a token
provider and never persisted.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from typing import Callable

import httpx

from config import HostedAgentConfig

logger = logging.getLogger(__name__)

TokenProvider = Callable[[str], str]


@dataclass
class ContainerFile:
    id: str
    filename: str | None
    bytes: int | None
    created_at: int | None
    source: str | None  # "user" / "assistant" when the API reports it
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "ContainerFile":
        return cls(
            id=d.get("id", ""),
            filename=d.get("filename") or d.get("path") or d.get("name"),
            bytes=d.get("bytes") or d.get("size"),
            created_at=d.get("created_at"),
            source=d.get("source"),
            raw=d,
        )


@dataclass
class PptxVerification:
    is_zip: bool
    opens_as_office: bool
    part_count: int
    has_presentation_xml: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.is_zip and self.opens_as_office


class ContainerError(RuntimeError):
    """Raised when a container REST call fails."""


class ContainerManager:
    """Thin REST client for the project-scoped container surface.

    Accepts an optional ``http_client`` so tests can inject a mock transport.
    """

    def __init__(
        self,
        config: HostedAgentConfig,
        token_provider: TokenProvider,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._cfg = config
        self._token = token_provider
        self._owns_client = http_client is None
        verify: bool | str = config.verify_tls
        if config.ca_bundle_path:
            verify = config.ca_bundle_path
        self._http = http_client or httpx.Client(
            timeout=config.request_timeout_seconds, verify=verify
        )

    # --- low level ----------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token(self._cfg.foundry_scope)}"}

    def _params(self) -> dict:
        return {"api-version": self._cfg.containers_api_version}

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # --- container lifecycle ------------------------------------------------
    def create_container(self, name: str = "hosted-agent-ci") -> str:
        """Create a client-owned container; return its id (``cntr_...``)."""
        resp = self._http.post(
            self._cfg.containers_url,
            headers=self._headers(),
            params=self._params(),
            json={"name": name},
        )
        if resp.status_code >= 400:
            raise ContainerError(
                f"create_container failed [{resp.status_code}]: {resp.text[:500]}"
            )
        cid = resp.json().get("id")
        if not cid:
            raise ContainerError(f"create_container returned no id: {resp.text[:300]}")
        logger.info("Created client-owned container %s", cid)
        return cid

    def ensure_container(self, existing_id: str | None, name: str = "hosted-agent-ci") -> str:
        """Return ``existing_id`` if provided, otherwise create a fresh one."""
        if existing_id:
            return existing_id
        if self._cfg.container_mode == "explicit" and self._cfg.explicit_container_id:
            return self._cfg.explicit_container_id
        return self.create_container(name)

    # --- files --------------------------------------------------------------
    def upload_file(
        self, container_id: str, filename: str, content: bytes
    ) -> ContainerFile:
        url = f"{self._cfg.containers_url}/{container_id}/files"
        resp = self._http.post(
            url,
            headers=self._headers(),
            params=self._params(),
            files={"file": (filename, content)},
        )
        if resp.status_code >= 400:
            raise ContainerError(
                f"upload_file failed [{resp.status_code}]: {resp.text[:500]}"
            )
        return ContainerFile.from_api(resp.json())

    def list_files(self, container_id: str) -> list[ContainerFile]:
        url = f"{self._cfg.containers_url}/{container_id}/files"
        resp = self._http.get(url, headers=self._headers(), params=self._params())
        if resp.status_code >= 400:
            raise ContainerError(
                f"list_files failed [{resp.status_code}]: {resp.text[:500]}"
            )
        body = resp.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        return [ContainerFile.from_api(d) for d in (data or [])]

    def download_file(self, container_id: str, file_id: str) -> bytes:
        url = f"{self._cfg.containers_url}/{container_id}/files/{file_id}/content"
        resp = self._http.get(url, headers=self._headers(), params=self._params())
        if resp.status_code >= 400:
            raise ContainerError(
                f"download_file failed [{resp.status_code}]: {resp.text[:300]}"
            )
        return resp.content

    # --- diffing / evidence -------------------------------------------------
    @staticmethod
    def new_assistant_files(
        before: list[ContainerFile], after: list[ContainerFile]
    ) -> list[ContainerFile]:
        """Files present after a turn that were not present before it.

        Prefers files the API marks ``source == 'assistant'``; falls back to a
        plain id-set diff when the source field is absent.
        """
        before_ids = {f.id for f in before}
        fresh = [f for f in after if f.id not in before_ids]
        assistant = [f for f in fresh if (f.source or "").lower() == "assistant"]
        return assistant or fresh


# ─── PPTX / Office package verification ─────────────────────────────────────


def verify_pptx_bytes(content: bytes) -> PptxVerification:
    """Verify a byte blob is a real PPTX (ZIP/OOXML), not model-fabricated text."""
    is_zip = content[:2] == b"PK"
    if not is_zip:
        return PptxVerification(
            is_zip=False,
            opens_as_office=False,
            part_count=0,
            has_presentation_xml=False,
            detail="Missing 'PK' ZIP magic header — not an Office package.",
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            has_pres = any(
                n.startswith("ppt/presentation.xml") for n in names
            )
            has_ct = "[Content_Types].xml" in names
            opens = has_ct and (has_pres or any(n.startswith("ppt/") for n in names))
            return PptxVerification(
                is_zip=True,
                opens_as_office=opens,
                part_count=len(names),
                has_presentation_xml=has_pres,
                detail=(
                    "Valid OOXML package"
                    if opens
                    else "ZIP opened but no ppt/ parts — not a PowerPoint file."
                ),
            )
    except zipfile.BadZipFile as exc:
        return PptxVerification(
            is_zip=True,
            opens_as_office=False,
            part_count=0,
            has_presentation_xml=False,
            detail=f"PK header present but ZIP is corrupt: {exc}",
        )
