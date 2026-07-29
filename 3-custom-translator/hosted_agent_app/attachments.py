"""Teams attachment abstraction for the hosted-agent adapter.

The existing Teams flow under ``publish-agent`` is NOT modified. This module
defines the *interfaces* the new hosted-agent adapter will use so a Teams
attachment (a PowerPoint template a user drops into the chat) can later be
ingested into the client-created Foundry container.

What is implemented here:
    * a transport-agnostic attachment abstraction (name/content-type/size/url);
    * extension + size validation for .pptx/.potx;
    * an ingestion helper that uploads validated bytes into a container;
    * a Bot Framework / Teams download hook that is DELIBERATELY not faked.

If the Teams/Bot Framework authorization cannot be exercised locally, the
download hook raises with the exact remaining integration step and the env vars
required, rather than pretending the download succeeded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

ALLOWED_TEMPLATE_EXTS = (".pptx", ".potx")
ALLOWED_TEMPLATE_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.presentationml.template",
    "application/vnd.ms-powerpoint.template.macroenabled.12",
    # Teams frequently sends a generic type; extension is the real gate.
    "application/octet-stream",
}


@dataclass
class AttachmentRef:
    """Transport-agnostic view of an inbound attachment."""

    name: str
    content_type: str | None
    size: int | None
    download_url: str | None

    @property
    def extension(self) -> str:
        dot = self.name.rfind(".")
        return self.name[dot:].lower() if dot != -1 else ""


class AttachmentDownloader(Protocol):
    """Downloads attachment bytes. Teams/Bot Framework impl injected later."""

    def download(self, ref: AttachmentRef) -> bytes: ...


class ContainerUploader(Protocol):
    """Uploads bytes into the client-created container (see ContainerManager)."""

    def upload_file(self, container_id: str, filename: str, content: bytes): ...
    def ensure_container(self, existing_id: str | None, name: str = ...) -> str: ...


class AttachmentValidationError(ValueError):
    pass


def validate_template_attachment(ref: AttachmentRef, max_bytes: int) -> None:
    """Validate extension and size. Raise AttachmentValidationError on failure."""
    if ref.extension not in ALLOWED_TEMPLATE_EXTS:
        raise AttachmentValidationError(
            f"Unsupported template type '{ref.extension or ref.name}'. "
            f"Allowed: {', '.join(ALLOWED_TEMPLATE_EXTS)}."
        )
    if ref.size is not None and ref.size > max_bytes:
        raise AttachmentValidationError(
            f"Template is {ref.size} bytes; limit is {max_bytes} bytes."
        )


def validate_template_bytes(filename: str, content: bytes, max_bytes: int) -> None:
    """Validate an already-downloaded template (extension + size + magic)."""
    ref = AttachmentRef(
        name=filename, content_type=None, size=len(content), download_url=None
    )
    validate_template_attachment(ref, max_bytes)
    # .potx/.pptx are OOXML ZIP packages — must start with the PK magic bytes.
    if content[:2] != b"PK":
        raise AttachmentValidationError(
            "File does not have a ZIP/OOXML 'PK' header — not a PowerPoint file."
        )


def ingest_attachment_to_container(
    ref: AttachmentRef,
    downloader: AttachmentDownloader,
    uploader: ContainerUploader,
    max_bytes: int,
    container_id: str | None = None,
) -> tuple[str, object]:
    """Validate -> download -> upload a Teams attachment into a container.

    Returns ``(container_id, uploaded_file)``. Raises on any failure; never
    fabricates a successful upload.
    """
    validate_template_attachment(ref, max_bytes)
    content = downloader.download(ref)
    validate_template_bytes(ref.name, content, max_bytes)
    cid = uploader.ensure_container(container_id)
    uploaded = uploader.upload_file(cid, ref.name, content)
    logger.info("Ingested Teams attachment %s into container %s", ref.name, cid)
    return cid, uploaded


class NotYetIntegratedError(NotImplementedError):
    """Raised by the placeholder Teams downloader to document the missing step."""


class TeamsBotFrameworkDownloader:
    """Placeholder Bot Framework downloader.

    Teams attachments arrive as ``Activity.attachments[]`` with a ``contentUrl``.
    Downloading them requires a Bot Framework connector token (the bot's app
    credential) or a Graph token for files hosted in SharePoint/OneDrive.

    This class documents — and refuses to fake — that step. Provide a real
    implementation wired to the bot's credentials before enabling Teams input.

    Required environment / wiring for the real implementation:
        * MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD (bot credentials), OR
        * a Graph token with Files.Read for SharePoint/OneDrive-hosted files;
        * outbound network access from the adapter to the Bot Framework /
          Graph download URL (respecting private-endpoint egress rules).
    """

    def download(self, ref: AttachmentRef) -> bytes:  # pragma: no cover - stub
        raise NotYetIntegratedError(
            "Teams attachment download is not wired locally. Implement "
            "TeamsBotFrameworkDownloader.download using the Bot Framework "
            "connector token (MICROSOFT_APP_ID/PASSWORD) or a Graph Files.Read "
            f"token to fetch '{ref.download_url}'. See module docstring."
        )
