# Copyright (c) Microsoft. All rights reserved.

"""A simple personal assistant on the Activity protocol.

A minimal Teams agent that chats through the **GitHub Copilot SDK** (which runs
its own model + tool-calling loop) and exposes a few custom tools: a to-do list
and reading files the user shares in the chat.

Hosted by ``azure-ai-agentserver-activity`` for the Foundry platform contract
and bridged to the M365 Agents SDK for activity processing and outbound channel
delivery (e.g. Microsoft Teams).
"""

import asyncio
import json
import logging
from os import environ

_LOG_LEVEL = environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s:%(lineno)d | %(message)s",
)
logger = logging.getLogger("github-copilot")

_RELEASE_LABEL = "rollout-baseline"

# Outbound card/file sends after a stream: attempts and linear backoff (seconds).
_UI_SEND_ATTEMPTS = 3
_UI_SEND_BACKOFF_S = 1.5

# Log the user's message and the agent's reply text (exported to App Insights
# when APPLICATIONINSIGHTS_CONNECTION_STRING is set). Set to "false" to log
# metadata only. Truncated to stay under App Insights' 32 KB trace limit.
_LOG_MESSAGE_CONTENT = environ.get("LOG_MESSAGE_CONTENT", "true").strip().lower() not in ("0", "false", "no")
_LOG_MESSAGE_MAX_CHARS = 30000


def _log_message_content(kind: str, activity, conversation_id: str, text: str) -> None:
    """Log one side of a turn (``USER_MESSAGE`` / ``AGENT_REPLY``) with its text."""
    if not _LOG_MESSAGE_CONTENT:
        return
    sender = getattr(activity, "from_property", None)
    truncated = len(text) > _LOG_MESSAGE_MAX_CHARS
    logger.info(
        "%s conversation_id=%s activity_id=%s user_aad_id=%s text_len=%d truncated=%s | %s",
        kind,
        conversation_id,
        getattr(activity, "id", None),
        getattr(sender, "aad_object_id", None),
        len(text),
        truncated,
        text[:_LOG_MESSAGE_MAX_CHARS],
    )

from azure.ai.agentserver.activity import ActivityAgentServerHost

import client as copilot_client
import files
import outfiles
import cards
import tools as _tools


def _current_call_id() -> str | None:
    """Per-request Foundry call id from the platform request context, or None.

    Available only on container protocol 2.0.0 in a hosted container; absent in
    local dev. Read inside the request-processing context (the message handler).
    """
    try:
        from azure.ai.agentserver.core import get_request_context
        return get_request_context().call_id
    except Exception:  # pylint: disable=broad-exception-caught
        return None


async def _deliver_ui(context, conversation_id, stream) -> None:
    """Drain model-requested UI (task board, generated files) and attach it.

    Tools can't send activities, so they queue requests (see tools.queue_ui);
    we render them here. On a streaming turn the attachments ride the final
    chunk; otherwise they go as separate messages.
    """
    from microsoft_agents.activity import Activity, ActivityTypes

    actions = _tools.drain_ui(conversation_id)
    turn_id = getattr(getattr(context, "activity", None), "id", None)
    call_id = _tools.get_call_id(conversation_id)
    call_id_present = bool(call_id)
    logger.info(
        "TURN_CORRELATION conversation_id=%s turn_id=%s call_id_present=%s action_count=%d",
        conversation_id,
        turn_id,
        call_id_present,
        len(actions),
    )

    def _attachment_meta(att):
        content = getattr(att, "content", None)
        size_in_bytes = None
        content_keys = ""
        if isinstance(content, dict):
            size_in_bytes = content.get("sizeInBytes")
            content_keys = ",".join(sorted(content.keys()))
        elif content is not None:
            try:
                serialized = json.dumps(content)
                size_in_bytes = len(serialized.encode("utf-8"))
            except Exception:  # pylint: disable=broad-exception-caught
                size_in_bytes = None
        return {
            "content_type": getattr(att, "content_type", None),
            "name": getattr(att, "name", None),
            "size_in_bytes": size_in_bytes,
            "content_keys": content_keys,
        }

    for action in actions:
        try:
            if action.get("type") == "task_board":
                tasks = _tools.get_tasks(conversation_id)
                att, lead = cards.tasks_card_attachment(conversation_id, tasks), ""
            elif action.get("type") == "consent":
                url = action.get("url") or ""
                if not url:
                    continue
                att = cards.consent_card_attachment(url, action.get("label") or "the data source")
                lead = ""
                logger.info(
                    "TOOLBOX_CONSENT_URL_EMITTED conversation_id=%s turn_id=%s call_id_present=%s url_prefix=%s",
                    conversation_id,
                    turn_id,
                    call_id_present,
                    url[:120],
                )
            elif action.get("type") == "file":
                # The model created the file itself (via its shell/python tools)
                # and handed us the path; read the bytes and offer them for
                # download. See tools._deliver_file.
                import os
                path = action.get("path") or ""
                with open(path, "rb") as fh:
                    data = fh.read()
                att, lead = outfiles.build_file_consent(os.path.basename(path) or "document.txt", data)
                logger.info(
                    "UI_FILE_PREPARED conversation_id=%s path=%s filename=%s bytes=%d",
                    conversation_id,
                    path,
                    os.path.basename(path) or "document.txt",
                    len(data),
                )
            else:
                continue
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("UI render failed: %s", exc, exc_info=True)
            continue

        if stream is not None:
            if lead:
                stream.queue_text_chunk(lead)
            stream.add_attachment(att)
            meta = _attachment_meta(att)
            logger.info(
                "UI_STREAM_ATTACHMENT_QUEUED conversation_id=%s turn_id=%s send_method=%s type=%s name=%s size_in_bytes=%s content_keys=%s",
                conversation_id,
                turn_id,
                "stream_attachment",
                meta["content_type"],
                meta["name"],
                meta["size_in_bytes"],
                meta["content_keys"],
            )
        else:
            # Lead text + card go in ONE activity so they succeed or fail together
            # (previously the text could land while the card send failed silently).
            # Retry with backoff: this send follows a burst of stream updates, so a
            # transient 429/5xx from the channel is the likely intermittent failure.
            meta = _attachment_meta(att)
            # Activity.text must be a str when present, so omit it for cards
            # with no lead (e.g. the toolbox sign-in card).
            out = Activity(type=ActivityTypes.message, attachments=[att])
            if lead:
                out.text = lead
            sent = False
            for attempt in range(1, _UI_SEND_ATTEMPTS + 1):
                try:
                    await context.send_activity(out)
                    logger.info(
                        "UI_ATTACHMENT_SENT conversation_id=%s turn_id=%s send_method=%s attempt=%d type=%s name=%s size_in_bytes=%s content_keys=%s",
                        conversation_id,
                        turn_id,
                        "standalone_message",
                        attempt,
                        meta["content_type"],
                        meta["name"],
                        meta["size_in_bytes"],
                        meta["content_keys"],
                    )
                    sent = True
                    break
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error(
                        "UI_ATTACHMENT_SEND_FAILED conversation_id=%s turn_id=%s send_method=%s attempt=%d/%d type=%s name=%s size_in_bytes=%s content_keys=%s error_type=%s error=%s",
                        conversation_id,
                        turn_id,
                        "standalone_message",
                        attempt,
                        _UI_SEND_ATTEMPTS,
                        meta["content_type"],
                        meta["name"],
                        meta["size_in_bytes"],
                        meta["content_keys"],
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
                    # A 4xx other than 429 is a rejection of the payload; retrying won't help.
                    status = getattr(exc, "status", None)
                    if isinstance(status, int) and 400 <= status < 500 and status != 429:
                        break
                    if attempt < _UI_SEND_ATTEMPTS:
                        await asyncio.sleep(_UI_SEND_BACKOFF_S * attempt)
            if not sent and action.get("type") == "file":
                # Tell the user instead of leaving them with no card and no message.
                try:
                    await context.send_activity(
                        f"I created **{meta['name']}** but the download card failed to load. "
                        "Please ask me to send it again.")
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error("UI_FALLBACK_SEND_FAILED conversation_id=%s error=%s", conversation_id, exc)


# Foundry overrides APPLICATIONINSIGHTS_CONNECTION_STRING in hosted containers
# with an invalid value, so deploy.ps1 ships ours as
# AGENT_APPINSIGHTS_CONNECTION_STRING. We pass it to the host AND write it back
# to the standard var: the Azure Monitor exporter re-reads the standard var
# itself and fails ("Invalid connection string") on the platform's value.
_APPINSIGHTS_CONNECTION_STRING = environ.get("AGENT_APPINSIGHTS_CONNECTION_STRING") or None
if _APPINSIGHTS_CONNECTION_STRING:
    environ["APPLICATIONINSIGHTS_CONNECTION_STRING"] = _APPINSIGHTS_CONNECTION_STRING
logger.info("APPINSIGHTS_EXPORT configured=%s", bool(_APPINSIGHTS_CONNECTION_STRING))

# Simple Teams agent auth model is the default.
host = ActivityAgentServerHost(applicationinsights_connection_string=_APPINSIGHTS_CONNECTION_STRING)
app = host.agent_app


@app.activity("message")
async def on_message(context, _state):
    """Chat turn: fold in any shared files, then answer via the Copilot SDK."""
    activity = context.activity
    conversation_id = activity.conversation.id if activity.conversation else "unknown"
    user_text = (activity.text or "").strip()
    # Capture the platform per-request call id (x-agent-foundry-call-id) so the
    # toolbox tools can forward it to the toolbox proxy — required for per-user
    # OAuth identity passthrough to resolve THIS user's consent/credentials.
    call_id = _current_call_id()
    _tools.set_call_id(conversation_id, call_id)
    logger.info(
        "TURN_CORRELATION_START conversation_id=%s turn_id=%s call_id_present=%s call_id_prefix=%s",
        conversation_id,
        getattr(activity, "id", None),
        bool(call_id),
        (call_id[:16] if call_id else "none"),
    )
    logger.info(
        "INBOUND_MESSAGE conversation_id=%s activity_id=%s channel=%s attachment_count=%d text_len=%d",
        conversation_id,
        getattr(activity, "id", None),
        getattr(activity, "channel_id", None),
        len(getattr(activity, "attachments", None) or []),
        len(user_text),
    )
    _log_message_content("USER_MESSAGE", activity, conversation_id, user_text)

    if user_text.casefold() == "/version":
        version = environ.get("FOUNDRY_AGENT_VERSION", "local")
        session_id = environ.get("FOUNDRY_AGENT_SESSION_ID", "local")
        await context.send_activity(
            f"agent_version={version}\n"
            f"release_label={_RELEASE_LABEL}\n"
            f"session_id={session_id}"
        )
        return

    if user_text.casefold() == "/debug-attachments":
        await context.send_activity(files.describe_attachments(activity))
        return

    # Try to start a streaming response; not all channels support it.
    stream = None
    try:
        stream = context.streaming_response
    except Exception:  # pylint: disable=broad-exception-caught
        stream = None

    def _progress(msg: str) -> None:
        if stream is not None:
            try:
                stream.queue_informative_update(msg)
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    # Download any shared files verbatim (no extraction) so we can hand their
    # raw paths to the model as attachments — it reads/analyzes them itself.
    # The authenticated connector (needed to fetch Copilot inline images) lives
    # on the turn state.
    connector = None
    try:
        connector = context.turn_state.get("ConnectorClient")
    except Exception:  # pylint: disable=broad-exception-caught
        connector = None
    shared_files = await files.download_shared_files(
        activity, conversation_id, on_progress=_progress, connector=connector
    )
    if shared_files:
        names = ", ".join(f["name"] for f in shared_files)
        prompt = user_text or f"Please read the shared file(s) ({names}) and give me the key insights and a short summary."
    else:
        prompt = user_text

    if not prompt:
        if stream is not None:
            try:
                await stream.end_stream()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        return

    # Some Bot Service channels do not enable streaming. Collect the same SDK
    # events into a regular message so those channels still receive a reply.
    if stream is None:
        chunks = []
        try:
            async for kind, chunk in copilot_client.ask_stream(conversation_id, prompt, shared_files):
                if kind == "delta" and chunk:
                    chunks.append(chunk)
                elif kind == "final" and chunk and not chunks:
                    chunks.append(chunk)
            reply = "".join(chunks) or "(no response)"
            await context.send_activity(reply)
            _log_message_content("AGENT_REPLY", activity, conversation_id, reply)
            await _deliver_ui(context, conversation_id, None)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("non-streaming response failed: %s", exc, exc_info=True)
            try:
                await context.send_activity("Sorry, I hit a problem answering that.")
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        return

    # Streaming path: forward progress + text chunks, finalize the stream, THEN
    # deliver model-requested UI as its OWN activities. A file-consent card ridden
    # on the streamed final activity fails with HTTP 400 (Teams rejects the large
    # base64-embedded attachment on a streamed send), so UI must go standalone.
    reply_chunks: list[str] = []
    try:
        async for kind, chunk in copilot_client.ask_stream(conversation_id, prompt, shared_files):
            if kind == "progress":
                stream.queue_informative_update(chunk)
            elif kind in ("delta", "final") and chunk:
                stream.queue_text_chunk(chunk)
                reply_chunks.append(chunk)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("stream failed: %s", exc, exc_info=True)
        try:
            stream.queue_text_chunk("Sorry, I hit a problem answering that.")
            reply_chunks.append("Sorry, I hit a problem answering that.")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    finally:
        try:
            await stream.end_stream()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Could not end stream: %s", exc)
        _log_message_content("AGENT_REPLY", activity, conversation_id, "".join(reply_chunks))
    # Deliver cards/files as standalone messages after the stream closes.
    try:
        await _deliver_ui(context, conversation_id, None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("UI delivery failed: %s", exc, exc_info=True)


@app.activity("invoke")
async def on_invoke(context, _state):
    """Handle invoke activities — file-consent responses and card actions."""
    activity = context.activity
    name = getattr(activity, "name", None)

    # Teams file-consent (outbound file download).
    if outfiles.is_file_consent_invoke(activity):
        try:
            await outfiles.handle_file_consent_invoke(context)
            from microsoft_agents.activity import Activity, ActivityTypes, InvokeResponse
            await context.send_activity(Activity(
                type=ActivityTypes.invoke_response,
                value=InvokeResponse(status=200),
            ))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("file consent handling failed: %s", exc, exc_info=True)
        return

    # Adaptive Card Universal Action (task board buttons).
    if name == cards.ADAPTIVE_ACTION_INVOKE:
        conversation_id = activity.conversation.id if activity.conversation else "unknown"
        parsed = cards.parse_action(activity)
        if parsed:
            verb, data = parsed
            task_id = data.get("taskId")
            try:
                if verb == "complete_task" and task_id:
                    _tools.complete_task_direct(conversation_id, task_id)
                elif verb == "delete_task" and task_id:
                    _tools.delete_task_direct(conversation_id, task_id)
                elif verb == "add_task":
                    # New task text comes from the card's Input.Text ('newTask').
                    val = getattr(activity, "value", None) or {}
                    if not isinstance(val, dict):
                        dump = getattr(val, "model_dump", None)
                        val = dump(by_alias=True) if callable(dump) else {}
                    action = val.get("action") or {}
                    if not isinstance(action, dict):
                        dump = getattr(action, "model_dump", None)
                        action = dump(by_alias=True) if callable(dump) else {}
                    inputs = action.get("data") or {}
                    new_title = (inputs.get("newTask") or "").strip() if isinstance(inputs, dict) else ""
                    if new_title:
                        _tools.add_task_direct(conversation_id, new_title)
                # refresh_tasks falls through to just re-render.
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.error("card action failed: %s", exc, exc_info=True)

            # Return a refreshed card as an IN-PLACE update via the invoke
            # response. Do NOT send a new card message — that would re-render a
            # card and (if it had an auto-refresh) loop. The invoke response
            # replaces the existing card in the chat.
            tasks = _tools.get_tasks(conversation_id)
            card = cards.build_tasks_card(conversation_id, tasks)
            resp = cards.card_invoke_response(card)
            try:
                from microsoft_agents.activity import Activity, ActivityTypes, InvokeResponse
                await context.send_activity(Activity(
                    type=ActivityTypes.invoke_response,
                    value=InvokeResponse(status=200, body=resp),
                ))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("could not send invoke response: %s", exc)
        return

    return


@app.activity("conversationUpdate")
async def on_members_added(context, _state):
    """Welcome new members."""
    members = context.activity.members_added or []
    for member in members:
        if member.id != context.activity.recipient.id:
            try:
                await context.send_activity(
                    "Hi, I'm your simple Teams assistant. Ask me anything, "
                    "or tell me to add a task."
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("Could not send welcome: %s", exc)


@app.error
async def on_error(context, error):
    """Handle unhandled errors."""
    logger.error("Handler error | error=%s", error, exc_info=True)
    try:
        await context.send_activity(f"Sorry, something went wrong: {error}")
    except Exception:  # pylint: disable=broad-exception-caught
        pass


if __name__ == "__main__":
    logger.info("Starting the GitHub Copilot SDK agent ...")
    host.run()
