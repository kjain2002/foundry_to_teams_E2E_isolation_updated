"""Tests for the hosted-agent Responses client (mocked HTTP)."""

from __future__ import annotations

import json

import httpx

from hosted_agent_client import HostedAgentClient


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_successful_turn_retains_raw(cfg, token_provider):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [{"type": "message", "content": [{"text": "hi"}]}],
            },
            headers={"apim-request-id": "req-123"},
        )

    client = HostedAgentClient(cfg, token_provider, http_client=_client(handler))
    result = client.create_response("hello", container_id="cntr_x")
    assert result.ok
    assert result.response_id == "resp_1"
    assert result.request_id == "req-123"
    assert result.raw["output"][0]["type"] == "message"
    # Container hint + code_interpreter tool binding applied.
    assert "USE_CONTAINER_ID=cntr_x" in captured["body"]["input"]
    assert captured["body"]["tools"][0]["container"] == "cntr_x"


def test_previous_response_id_chained(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["previous_response_id"] == "resp_prev"
        return httpx.Response(200, json={"id": "resp_2", "status": "completed", "output": []})

    client = HostedAgentClient(cfg, token_provider, http_client=_client(handler))
    result = client.create_response("again", previous_response_id="resp_prev")
    assert result.ok


def test_http_error_surfaced(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    client = HostedAgentClient(cfg, token_provider, http_client=_client(handler))
    result = client.create_response("hello")
    assert not result.ok
    assert result.http_status == 500
    assert "server exploded" in result.error


def test_timeout_surfaced(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    client = HostedAgentClient(cfg, token_provider, http_client=_client(handler))
    result = client.create_response("hello")
    assert not result.ok
    assert result.http_status is None
    assert "timed out" in result.error.lower()


def test_no_container_hint_when_absent(cfg, token_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "USE_CONTAINER_ID" not in body["input"]
        assert "tools" not in body
        return httpx.Response(200, json={"id": "r", "status": "completed", "output": []})

    client = HostedAgentClient(cfg, token_provider, http_client=_client(handler))
    assert client.create_response("plain").ok
