"""Streamlit chat UI for the hosted Foundry agent (parallel/compat build).

This is a NEW, self-contained app — it does not import from, or modify, the
sibling ``streamlit_app`` publisher or the read-only ``publish-agent`` tree.

Flow:
    User -> Streamlit -> hosted Foundry agent Responses endpoint
         -> an MCP data tool (Foundry toolbox) and/or hosted Code Interpreter
         -> client-created container -> generated files via container file API.

Guarantees surfaced in the UI:
    * raw Responses JSON is retained and viewable/downloadable;
    * MCP tool calls and Code Interpreter calls are separately identified;
    * a "file created" claim is shown ONLY with container-file evidence.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from activity import parse_activity, summarize, extract_text, extract_consent_link
from attachments import AttachmentValidationError, validate_template_bytes
from auth import get_current_user, get_token_provider
from config import get_config
from containers import ContainerManager, verify_pptx_bytes
from hosted_agent_client import HostedAgentClient
from logging_utils import TurnDiagnostics, redact_consent_link
from ppt_skill import build_ppt_instructions, looks_like_ppt_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title="Hosted Foundry Agent — Chat & Verify",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULTS = {
    "messages": [],            # list[{role, content, turn_index}]
    "conversation_id": None,   # stable per chat session
    "previous_response_id": None,
    "container_id": None,
    "template_ref": None,      # container path/id of uploaded template
    "template_name": None,
    "turns": [],               # list[dict] per-turn record for the debug panel
    "turn_index": 0,
}
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)
if st.session_state["conversation_id"] is None:
    st.session_state["conversation_id"] = f"conv-{uuid.uuid4().hex[:12]}"


def _reset_chat() -> None:
    for k, v in DEFAULTS.items():
        st.session_state[k] = ([] if isinstance(v, list) else v)
    st.session_state["conversation_id"] = f"conv-{uuid.uuid4().hex[:12]}"


# ─── Auth + clients ─────────────────────────────────────────────────────────

cfg = get_config()
user = get_current_user()
if not user:
    st.title("Hosted Foundry Agent — Chat & Verify")
    st.warning(
        "Not signed in. In local dev set `AUTH_MODE=local` and run `az login`. "
        "In Container Apps, Easy Auth should redirect you automatically."
    )
    st.stop()

token_provider = get_token_provider()


@st.cache_resource(show_spinner=False)
def _clients():
    # Cached across reruns within the session. Token provider is re-read lazily.
    return (
        HostedAgentClient(cfg, token_provider),
        ContainerManager(cfg, token_provider),
    )


agent_client, container_mgr = _clients()


# ─── Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown(f"**{user.name}**")
    st.caption(user.upn)
    st.divider()

    st.subheader("Session")
    st.caption(f"Conversation: `{st.session_state['conversation_id']}`")
    if st.session_state["container_id"]:
        st.caption(f"Container: `{st.session_state['container_id']}`")
    if st.button("🆕 New chat / reset", use_container_width=True):
        _reset_chat()
        st.rerun()

    st.divider()
    st.subheader("PowerPoint template (optional)")
    if st.session_state["template_name"]:
        st.success(f"Template attached: **{st.session_state['template_name']}**")
        if st.button("Clear template", use_container_width=True):
            st.session_state["template_ref"] = None
            st.session_state["template_name"] = None
            st.rerun()
    else:
        st.caption("No template — deck will use an original design.")

    uploaded = st.file_uploader(
        "Upload .pptx / .potx", type=["pptx", "potx"], key="tpl_uploader"
    )
    if uploaded is not None and st.button("Attach template", use_container_width=True):
        try:
            content = uploaded.getvalue()
            validate_template_bytes(uploaded.name, content, cfg.max_template_bytes)
            cid = container_mgr.ensure_container(st.session_state["container_id"])
            st.session_state["container_id"] = cid
            up = container_mgr.upload_file(cid, uploaded.name, content)
            st.session_state["template_ref"] = up.filename or up.id
            st.session_state["template_name"] = uploaded.name
            st.success(f"Uploaded to container `{cid}` as `{up.id}`.")
            st.rerun()
        except AttachmentValidationError as exc:
            st.error(f"Template rejected: {exc}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Upload failed: {exc}")

    st.divider()
    st.subheader("Style (no-template mode)")
    style_instructions = st.text_area(
        "Design style instructions",
        placeholder="e.g. executive, Microsoft-style, brand colours #742774, "
        "spacious layout, minimal text",
        height=80,
        key="style_instructions",
    )

    st.divider()
    st.caption(
        f"Agent: `{cfg.hosted_agent_name or cfg.agent_responses_url}`  \n"
        f"Container mode: `{cfg.container_mode}`  \n"
        f"Timeout: `{cfg.request_timeout_seconds}s`"
    )


# ─── Header ─────────────────────────────────────────────────────────────────

st.title("Hosted Foundry Agent — Chat & Verify")
st.caption(
    "Chats through the hosted agent's Responses endpoint. Tool activity is shown "
    "from the raw response — Code Interpreter is verified against the container "
    "file API, never inferred from the assistant's text."
)


# ─── Turn detail renderer ───────────────────────────────────────────────────

def _render_turn_details(rec: dict) -> None:
    """Render activity, evidence and downloads for one assistant turn."""
    summary = rec["summary"]

    cols = st.columns(3)
    cols[0].metric("MCP tool calls", summary["mcp_tool_call_count"])
    cols[1].metric(
        "Code Interpreter",
        "yes" if summary["has_code_interpreter"] else "no",
    )
    cols[2].metric("HTTP", rec.get("http_status") or "—")

    # Explicit MCP-tool vs Code Interpreter distinction.
    if summary["mcp_tool_call_count"] and not summary["has_code_interpreter"]:
        st.info(
            "MCP tool function calls are present, but **no Code Interpreter "
            "activity** item was returned this turn."
        )
    elif not summary["has_code_interpreter"] and not summary["mcp_tool_call_count"]:
        st.caption("No tool activity items in this response.")

    if summary.get("consent_link"):
        st.warning(f"Authorization required: {summary['consent_link']}")

    # Evidence-gated file section.
    for f in rec.get("generated_files", []):
        verified = f.get("verification")
        label = f"⬇️ {f['filename'] or f['id']}"
        st.download_button(
            label=label,
            data=bytes.fromhex(f["hex"]) if f.get("hex") else b"",
            file_name=f["filename"] or f"{f['id']}.bin",
            mime="application/octet-stream",
            key=f"dl-{rec['turn_index']}-{f['id']}",
            disabled=not f.get("hex"),
        )
        if verified is not None:
            if verified["ok"]:
                st.success(f"Verified Office package — {verified['detail']}")
            else:
                st.error(f"Verification failed — {verified['detail']}")

    if rec.get("ci_claim_without_evidence"):
        st.error(
            "⚠️ The assistant text mentions generating a file, but there is no "
            "Code Interpreter activity item AND no new container file. Treating "
            "this as **not** generated."
        )

    with st.expander("Raw response activity (JSON)"):
        st.json(rec.get("activity_items", []))
    with st.expander("Full raw Responses JSON"):
        st.json(rec.get("raw") or {"note": "no body"})
        st.download_button(
            "Download debug JSON",
            data=rec["debug_json"],
            file_name=f"turn-{rec['turn_index']}-debug.json",
            mime="application/json",
            key=f"dbg-{rec['turn_index']}",
        )
    if rec.get("error"):
        st.error(f"Error: {rec['error']}")


# ─── Turn handling ──────────────────────────────────────────────────────────

def _handle_turn(user_text: str) -> None:
    idx = st.session_state["turn_index"]
    is_ppt = looks_like_ppt_request(user_text)

    container_id = st.session_state["container_id"]
    instructions = None
    files_before = []

    # For PPT requests (or explicit/auto container modes) make sure a
    # client-created container exists and pass the PPT skill instructions.
    if is_ppt or cfg.container_mode in {"auto", "explicit"}:
        try:
            container_id = container_mgr.ensure_container(container_id)
            st.session_state["container_id"] = container_id
            files_before = container_mgr.list_files(container_id)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Container preparation failed: {exc}")

    if is_ppt:
        instructions = build_ppt_instructions(
            has_template=bool(st.session_state["template_ref"]),
            style_instructions=st.session_state.get("style_instructions"),
            template_ref=st.session_state["template_ref"],
        )

    result = agent_client.create_response(
        user_text=user_text,
        previous_response_id=st.session_state["previous_response_id"],
        container_id=container_id,
        template_ref=st.session_state["template_ref"],
        instructions=instructions,
    )

    items = parse_activity(result.raw) if result.raw else []
    summary = summarize(items, cfg.mcp_server_label)
    assistant_text = extract_text(result.raw) or (
        result.error or "*(no text returned)*"
    )

    # Only advance the conversation chain on a real, successful text turn
    # without a pending consent request.
    consent = extract_consent_link(items)
    if result.ok and result.response_id and not consent:
        st.session_state["previous_response_id"] = result.response_id

    # ── Code Interpreter evidence gate ──────────────────────────────────
    generated_files: list[dict] = []
    ci_claim_without_evidence = False
    if container_id and files_before is not None:
        try:
            files_after = container_mgr.list_files(container_id)
            new_files = ContainerManager.new_assistant_files(files_before, files_after)
            for nf in new_files:
                content = b""
                verification = None
                try:
                    content = container_mgr.download_file(container_id, nf.id)
                    if (nf.filename or "").lower().endswith(".pptx"):
                        v = verify_pptx_bytes(content)
                        verification = {"ok": v.ok, "detail": v.detail}
                except Exception as exc:  # noqa: BLE001
                    verification = {"ok": False, "detail": f"download failed: {exc}"}
                generated_files.append(
                    {
                        "id": nf.id,
                        "filename": nf.filename,
                        "hex": content.hex() if content else "",
                        "verification": verification,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-turn file listing failed: %s", exc)

    # If the text claims a file but there is neither CI activity nor a new file.
    text_claims_file = any(
        kw in (assistant_text or "").lower()
        for kw in ("created", "generated", "attached", "download", ".pptx")
    )
    if text_claims_file and not summary.has_code_interpreter and not generated_files:
        ci_claim_without_evidence = True

    diag = TurnDiagnostics(
        conversation_id=st.session_state["conversation_id"],
        session_id=st.session_state["conversation_id"],
        request_id=result.request_id,
        http_status=result.http_status,
        response_status=result.response_status,
        response_id=result.response_id,
        activity_item_types=[it.type for it in items],
        activity_item_ids=[it.id for it in items if it.id],
        container_id=container_id,
        has_code_interpreter=summary.has_code_interpreter,
        mcp_tool_call_count=summary.mcp_tool_call_count,
        generated_file_names=[f["filename"] or f["id"] for f in generated_files],
        generated_file_ids=[f["id"] for f in generated_files],
        error=result.error,
        duration_seconds=result.duration_seconds,
    )
    diag.emit()

    activity_items = [
        {
            "type": it.type,
            "id": it.id,
            "status": it.status,
            "name": it.name,
            "server_label": it.server_label,
            "error": it.error,
        }
        for it in items
    ]
    summary_dict = summary.as_dict()
    summary_dict["consent_link"] = redact_consent_link(summary_dict.get("consent_link"))

    rec = {
        "turn_index": idx,
        "http_status": result.http_status,
        "summary": summary_dict,
        "activity_items": activity_items,
        "raw": result.raw,
        "generated_files": generated_files,
        "ci_claim_without_evidence": ci_claim_without_evidence,
        "error": result.error,
        "debug_json": diag.to_json(),
    }

    st.session_state["messages"].append(
        {"role": "user", "content": user_text, "turn_index": idx}
    )
    st.session_state["messages"].append(
        {"role": "assistant", "content": assistant_text, "turn_index": idx}
    )
    st.session_state["turns"].append(rec)
    st.session_state["turn_index"] = idx + 1


# ─── Render history ─────────────────────────────────────────────────────────

for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            rec = next(
                (
                    t
                    for t in st.session_state["turns"]
                    if t.get("turn_index") == msg.get("turn_index")
                ),
                None,
            )
            if rec:
                _render_turn_details(rec)


prompt = st.chat_input("Ask the hosted agent… (e.g. 'build a 5-slide PPTX on Q3 sales')")
if prompt:
    _handle_turn(prompt)
    st.rerun()
