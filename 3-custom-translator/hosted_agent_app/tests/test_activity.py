"""Tests for the Responses activity parser (evidence-over-inference)."""

from __future__ import annotations

from activity import (
    extract_consent_link,
    extract_text,
    has_code_interpreter_activity,
    parse_activity,
    mcp_tool_calls,
    summarize,
)


def _resp(output):
    return {"id": "resp_1", "status": "completed", "output": output}


def test_no_tool_activity():
    raw = _resp(
        [{"type": "message", "content": [{"text": "hello there"}]}]
    )
    items = parse_activity(raw)
    assert not has_code_interpreter_activity(items)
    assert mcp_tool_calls(items) == []
    assert extract_text(raw) == "hello there"


def test_mcp_tool_function_call_without_code_interpreter():
    raw = _resp(
        [
            {
                "type": "function_call",
                "id": "fc_1",
                "status": "completed",
                "name": "query",
                "server_label": "mcp",
                "arguments": "{}",
            },
            {"type": "message", "content": [{"text": "ran a query"}]},
        ]
    )
    items = parse_activity(raw)
    assert len(mcp_tool_calls(items)) == 1
    assert not has_code_interpreter_activity(items)
    s = summarize(items)
    assert s.mcp_tool_call_count == 1
    assert s.has_code_interpreter is False


def test_code_interpreter_activity_present():
    raw = _resp(
        [
            {"type": "code_interpreter_call", "id": "ci_1", "status": "completed"},
            {"type": "message", "content": [{"text": "made a deck"}]},
        ]
    )
    items = parse_activity(raw)
    assert has_code_interpreter_activity(items)


def test_text_claim_does_not_imply_code_interpreter():
    # Assistant *says* it ran CI, but there is no code_interpreter_call item.
    raw = _resp(
        [{"type": "message", "content": [{"text": "I generated slides.pptx"}]}]
    )
    items = parse_activity(raw)
    assert has_code_interpreter_activity(items) is False


def test_consent_request_extracted():
    raw = _resp(
        [
            {
                "type": "oauth_consent_request",
                "id": "oc_1",
                "consent_link": "https://login/consent?x=1",
            }
        ]
    )
    items = parse_activity(raw)
    assert extract_consent_link(items) == "https://login/consent?x=1"


def test_error_item():
    raw = _resp([{"type": "error", "error": {"message": "boom"}}])
    items = parse_activity(raw)
    s = summarize(items)
    assert s.error_messages == ["boom"]
