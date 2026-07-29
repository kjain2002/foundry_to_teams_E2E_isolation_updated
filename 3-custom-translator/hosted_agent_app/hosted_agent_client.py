"""Hosted-agent client — calls the deployed Foundry Responses endpoint.

Raw REST (httpx) is used deliberately so the *complete* Responses JSON is
retained for inspection (the OpenAI SDK hides the envelope). Conversation
continuity uses the Responses ``previous_response_id`` chain.

Features:
    * conversation memory (previous_response_id) held by the caller;
    * configurable timeout, private-endpoint / custom-CA friendly;
    * full raw JSON + HTTP status + error surfaced on every turn;
    * optional Code Interpreter binding to a client-created container;
    * optional template file reference passed to the agent.

No secrets are logged; the bearer token is fetched per call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from config import HostedAgentConfig

logger = logging.getLogger(__name__)

TokenProvider = Callable[[str], str]


@dataclass
class AgentTurnResult:
    """Everything one turn produced — success or failure."""

    ok: bool
    http_status: int | None
    response_id: str | None
    response_status: str | None
    raw: dict | None
    error: str | None = None
    duration_seconds: float | None = None
    request_id: str | None = None
    # The exact input text that was sent (with any container hint applied).
    sent_input: str = ""

    @property
    def output(self) -> list:
        if not self.raw:
            return []
        return self.raw.get("output", []) or []


class HostedAgentClient:
    """REST client for the hosted agent's Responses endpoint.

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

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # ------------------------------------------------------------------ build
    def _headers(self, user_token: str | None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token(self._cfg.foundry_scope)}",
            "Content-Type": "application/json",
        }
        # Forward the caller identity so MCP tools run as that user, when given.
        if user_token:
            headers["x-ms-user-token"] = f"Bearer {user_token}"
        return headers

    def build_input(
        self, user_text: str, container_id: str | None, template_ref: str | None
    ) -> str:
        """Compose the message text, injecting the container / template hints."""
        prefix_lines: list[str] = []
        if container_id and self._cfg.send_container_hint:
            prefix_lines.append(f"USE_CONTAINER_ID={container_id}")
        if template_ref:
            prefix_lines.append(f"USE_TEMPLATE_FILE={template_ref}")
        if not prefix_lines:
            return user_text
        return "\n".join(prefix_lines) + "\n\n" + user_text

    def build_tools(self, container_id: str | None) -> list[dict] | None:
        """Optionally bind a Code Interpreter tool to the explicit container."""
        if not (container_id and self._cfg.bind_code_interpreter_tool):
            return None
        return [
            {
                "type": "code_interpreter",
                "container": container_id,
            }
        ]

    # ------------------------------------------------------------------- call
    def create_response(
        self,
        user_text: str,
        previous_response_id: str | None = None,
        container_id: str | None = None,
        template_ref: str | None = None,
        instructions: str | None = None,
        user_token: str | None = None,
    ) -> AgentTurnResult:
        """Send one user turn to the hosted agent; return an AgentTurnResult."""
        import time

        sent_input = self.build_input(user_text, container_id, template_ref)
        payload: dict[str, Any] = {"input": sent_input}
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if instructions:
            payload["instructions"] = instructions
        tools = self.build_tools(container_id)
        if tools:
            payload["tools"] = tools
        if self._cfg.model:
            payload["model"] = self._cfg.model

        start = time.monotonic()
        try:
            resp = self._http.post(
                self._cfg.responses_url,
                headers=self._headers(user_token),
                json=payload,
            )
        except httpx.TimeoutException as exc:
            return AgentTurnResult(
                ok=False,
                http_status=None,
                response_id=None,
                response_status=None,
                raw=None,
                error=f"Request timed out after {self._cfg.request_timeout_seconds}s: {exc}",
                duration_seconds=time.monotonic() - start,
                sent_input=sent_input,
            )
        except httpx.HTTPError as exc:
            return AgentTurnResult(
                ok=False,
                http_status=None,
                response_id=None,
                response_status=None,
                raw=None,
                error=f"HTTP transport error: {exc}",
                duration_seconds=time.monotonic() - start,
                sent_input=sent_input,
            )

        duration = time.monotonic() - start
        request_id = resp.headers.get("apim-request-id") or resp.headers.get(
            "x-request-id"
        )

        if resp.status_code >= 400:
            body_preview = resp.text[:800]
            return AgentTurnResult(
                ok=False,
                http_status=resp.status_code,
                response_id=None,
                response_status=None,
                raw=_safe_json(resp),
                error=f"Responses API returned {resp.status_code}: {body_preview}",
                duration_seconds=duration,
                request_id=request_id,
                sent_input=sent_input,
            )

        raw = _safe_json(resp)
        if raw is None:
            return AgentTurnResult(
                ok=False,
                http_status=resp.status_code,
                response_id=None,
                response_status=None,
                raw=None,
                error="Response body was not valid JSON.",
                duration_seconds=duration,
                request_id=request_id,
                sent_input=sent_input,
            )

        return AgentTurnResult(
            ok=True,
            http_status=resp.status_code,
            response_id=raw.get("id"),
            response_status=raw.get("status"),
            raw=raw,
            duration_seconds=duration,
            request_id=request_id,
            sent_input=sent_input,
        )


def _safe_json(resp: httpx.Response) -> dict | None:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return None
