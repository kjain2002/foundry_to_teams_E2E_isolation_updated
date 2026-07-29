"""Tests for the container manager + PPTX verification (mocked HTTP)."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from containers import ContainerFile, ContainerManager, verify_pptx_bytes


def _make_pptx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("ppt/presentation.xml", "<presentation/>")
    return buf.getvalue()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_create_list_download(cfg, token_provider):
    pptx = _make_pptx_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/containers?api-version=2025-04-01-preview"):
            return httpx.Response(200, json={"id": "cntr_abc"})
        if request.method == "GET" and "/files/f1/content" in url:
            return httpx.Response(200, content=pptx)
        if request.method == "GET" and "/files" in url:
            return httpx.Response(
                200,
                json={"data": [{"id": "f1", "filename": "deck.pptx", "source": "assistant"}]},
            )
        return httpx.Response(404, json={"error": "unexpected"})

    mgr = ContainerManager(cfg, token_provider, http_client=_client(handler))
    cid = mgr.create_container()
    assert cid == "cntr_abc"
    files = mgr.list_files(cid)
    assert files[0].filename == "deck.pptx"
    content = mgr.download_file(cid, "f1")
    assert verify_pptx_bytes(content).ok


def test_upload_file(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "f9", "filename": "brand.potx"})

    mgr = ContainerManager(cfg, token_provider, http_client=_client(handler))
    up = mgr.upload_file("cntr_abc", "brand.potx", b"PKxx")
    assert up.id == "f9"


def test_new_assistant_files_diff():
    before = [ContainerFile("f1", "a", None, None, "user", {})]
    after = [
        ContainerFile("f1", "a", None, None, "user", {}),
        ContainerFile("f2", "b.pptx", None, None, "assistant", {}),
    ]
    fresh = ContainerManager.new_assistant_files(before, after)
    assert [f.id for f in fresh] == ["f2"]


def test_verify_pptx_rejects_plaintext():
    v = verify_pptx_bytes(b"this is not a zip")
    assert not v.ok
    assert "PK" in v.detail


def test_verify_pptx_rejects_non_office_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    v = verify_pptx_bytes(buf.getvalue())
    assert v.is_zip and not v.opens_as_office


def test_create_container_http_error(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    mgr = ContainerManager(cfg, token_provider, http_client=_client(handler))
    with pytest.raises(Exception):
        mgr.create_container()
