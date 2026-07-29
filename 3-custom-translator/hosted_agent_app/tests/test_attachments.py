"""Tests for the Teams attachment abstraction + validation."""

from __future__ import annotations

import pytest

from attachments import (
    AttachmentRef,
    AttachmentValidationError,
    NotYetIntegratedError,
    TeamsBotFrameworkDownloader,
    ingest_attachment_to_container,
    validate_template_attachment,
    validate_template_bytes,
)


def _ref(name, size=1000):
    return AttachmentRef(name=name, content_type=None, size=size, download_url="https://x")


def test_valid_extension_accepted():
    validate_template_attachment(_ref("brand.pptx"), max_bytes=10_000)
    validate_template_attachment(_ref("brand.potx"), max_bytes=10_000)


def test_invalid_extension_rejected():
    with pytest.raises(AttachmentValidationError):
        validate_template_attachment(_ref("notes.txt"), max_bytes=10_000)


def test_oversize_rejected():
    with pytest.raises(AttachmentValidationError):
        validate_template_attachment(_ref("big.pptx", size=999_999), max_bytes=10)


def test_validate_bytes_requires_pk_header():
    with pytest.raises(AttachmentValidationError):
        validate_template_bytes("deck.pptx", b"not a zip", max_bytes=10_000)
    validate_template_bytes("deck.pptx", b"PK\x03\x04rest", max_bytes=10_000)


class _FakeDownloader:
    def download(self, ref):
        return b"PK\x03\x04fake-office"


class _FakeUploader:
    def __init__(self):
        self.uploaded = []

    def ensure_container(self, existing_id, name="hosted-agent-ci"):
        return existing_id or "cntr_new"

    def upload_file(self, container_id, filename, content):
        self.uploaded.append((container_id, filename, content))
        return {"id": "f1", "filename": filename}


def test_ingest_flow_uploads_to_container():
    uploader = _FakeUploader()
    cid, up = ingest_attachment_to_container(
        _ref("brand.pptx"),
        downloader=_FakeDownloader(),
        uploader=uploader,
        max_bytes=10_000,
    )
    assert cid == "cntr_new"
    assert uploader.uploaded[0][1] == "brand.pptx"


def test_placeholder_downloader_does_not_fake_success():
    with pytest.raises(NotYetIntegratedError):
        TeamsBotFrameworkDownloader().download(_ref("brand.pptx"))
