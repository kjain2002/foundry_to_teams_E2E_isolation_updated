"""Tests for structured logging redaction."""

from __future__ import annotations

import json

from logging_utils import TurnDiagnostics, redact, redact_consent_link


def test_redact_bearer_in_string():
    assert redact("Authorization: Bearer abc.def.ghi") == (
        "Authorization: Bearer [REDACTED]"
    )


def test_redact_secret_keys_in_dict():
    out = redact({"authorization": "Bearer x", "token": "y", "safe": "ok"})
    assert out["authorization"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert out["safe"] == "ok"


def test_redact_consent_link_drops_query():
    link = "https://login.microsoftonline.com/consent?state=secret&code=abc"
    red = redact_consent_link(link)
    assert "state=secret" not in red
    assert red.startswith("https://login.microsoftonline.com/consent")


def test_turn_diagnostics_json_roundtrip():
    diag = TurnDiagnostics(
        conversation_id="conv-1",
        session_id="conv-1",
        http_status=200,
        response_status="completed",
        response_id="resp_1",
        activity_item_types=["function_call", "code_interpreter_call"],
        container_id="cntr_x",
        has_code_interpreter=True,
        mcp_tool_call_count=1,
        generated_file_names=["deck.pptx"],
        generated_file_ids=["f1"],
        duration_seconds=1.23,
    )
    data = json.loads(diag.to_json())
    assert data["container_id"] == "cntr_x"
    assert data["has_code_interpreter"] is True
    # No secret-looking keys leaked.
    assert "authorization" not in json.dumps(data).lower()


def test_diagnostics_error_is_redacted():
    diag = TurnDiagnostics(
        conversation_id="c",
        session_id="c",
        error="failed with Bearer eyJ0.abc.def token",
    )
    data = diag.to_dict()
    assert "Bearer [REDACTED]" in data["error"]
