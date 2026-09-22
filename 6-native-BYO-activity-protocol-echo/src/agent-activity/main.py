# Copyright (c) Microsoft. All rights reserved.

"""Activity-protocol bridge agent: Teams <-> existing Foundry agent with OBO.

Hosted by ``azure-ai-agentserver-activity`` for the Foundry platform contract
and bridged to the M365 Agents SDK for activity processing and outbound Teams
delivery. This does NOT rebuild the agent brain: it forwards each turn to the
EXISTING deployed agent's Responses endpoint, attaching the signed-in user's
token as ``x-ms-user-token`` so the Starburst toolbox runs under the user's
identity (the piece the native publish in folder 5 was missing).

User token acquisition is native: an M365 Agents SDK OAuth AuthHandler bound to
an Azure Bot OAuth connection sends the Teams SSO card and returns the OBO token
for the MCP resource scope.
"""

import asyncio
import logging

from azure.ai.agentserver.activity import ActivityAgentServerHost
from dotenv import load_dotenv
from microsoft_agents.activity import Activity, ActivityTypes, Attachment

import config
import outfiles
import prefs
import sessions
from foundry_bridge import FoundryBridge
from token_claims import log_claims

load_dotenv()

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("agent-activity")

host = ActivityAgentServerHost()
app = host.agent_app

_bridge = FoundryBridge()

# conversation_id -> agent session id (its $HOME holds uploaded/generated files).
_sessions: dict[str, str] = {}

# conversation_id -> original user text, stashed while awaiting consent (Retry).
_pending_consent: dict[str, str] = {}

_ADAPTIVE_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


async def _resolve_user_token(context) -> str | None:
    """Get the signed-in user's OBO token for the MCP resource scope.

    Uses the M365 Agents SDK Authorization bound to the configured Azure Bot
    OAuth connection. If the user isn't signed in, the SDK sends the SSO card
    and this returns None for that turn. Requires an AuthHandler + Bot OAuth
    connection to be configured (see azure.yaml / bot setup).
    """
    auth = getattr(app, "auth", None)
    if auth is None:
        logger.warning("[AUTH] app.auth is not configured; no user token")
        return None
    scopes = [config.MCP_USER_SCOPE] if config.MCP_USER_SCOPE else None
    try:
        resp = await auth.exchange_token(
            context,
            scopes=scopes,
            exchange_connection=config.OBO_CONNECTION_NAME,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[AUTH] exchange_token failed: %s", exc)
        return None
    token = getattr(resp, "token", None)
    log_claims("user-obo", token)
    return token


async def _keep_typing(context) -> None:
    """Show a Teams typing indicator every few seconds until cancelled."""
    try:
        while True:
            await context.send_activity(Activity(type=ActivityTypes.typing))
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        pass
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("typing indicator failed", exc_info=True)


# (session_id, filename) -> cached bytes between the consent card and Accept.
_pending_files: dict[tuple[str, str], bytes] = {}


async def _fetch_generated_bytes(session_id: str, filename: str) -> bytes:
    cached = _pending_files.get((session_id, filename))
    if cached is not None:
        return cached
    return await sessions.download_session_file(
        _bridge.credential,
        config.FOUNDRY_PROJECT_ENDPOINT,
        config.TARGET_AGENT_NAME,
        session_id,
        filename,
    )


async def _deliver_generated_file(context, session_id: str, name: str) -> None:
    """Offer a generated file to Teams via the file-consent (download) flow."""
    try:
        data = await sessions.download_session_file(
            _bridge.credential,
            config.FOUNDRY_PROJECT_ENDPOINT,
            config.TARGET_AGENT_NAME,
            session_id,
            name,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("could not fetch generated file %s: %s", name, exc)
        return
    _pending_files[(session_id, name)] = data
    att, lead = outfiles.build_file_consent(name, session_id, len(data))
    try:
        await context.send_activity(lead)
        await context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[att])
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("could not send file consent for %s: %s", name, exc)


def _build_consent_card(consent_link: str) -> Attachment:
    """Adaptive Card consent prompt: open-link button + Retry (re-runs message)."""
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": "Access required", "weight": "Bolder", "size": "Medium"},
            {"type": "TextBlock", "text": "I need your consent before I can run that. Open the consent link, complete it, then choose Retry.", "wrap": True},
        ],
        "actions": [
            {"type": "Action.OpenUrl", "title": "Open consent link", "url": consent_link},
            {"type": "Action.Submit", "title": "Retry", "data": {"kind": "consent_retry"}},
        ],
    }
    return Attachment(content_type=_ADAPTIVE_CONTENT_TYPE, content=card)


@app.activity("message")
async def on_message(context, state):
    """Forward the user's message to the existing agent with the user's token."""
    activity = context.activity
    user_text = (activity.text or "").strip()
    has_attachments = bool(getattr(activity, "attachments", None))
    has_value = bool(getattr(activity, "value", None))
    if not user_text and not has_attachments and not has_value:
        return
    conversation_id = activity.conversation.id if activity.conversation else "unknown"
    from_prop = getattr(activity, "from_property", None)
    user_id = getattr(from_prop, "id", None) or "unknown"

    # TEMP DEBUG: surface exactly what attachment types Teams delivered, so a
    # manifest gap (0 attachments) is distinguishable from a parser miss (typed).
    _atts = getattr(activity, "attachments", None) or []
    try:
        _cts = ", ".join((getattr(a, "content_type", None) or "?") for a in _atts) or "none"
        await context.send_activity(f"[debug] attachments={len(_atts)}: {_cts}")
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    # Preference poll submit (Adaptive Card) - save and stop.
    submitted = prefs.parse_prefs_submit(activity)
    if submitted is not None:
        prefs.set_prefs(user_id, submitted)
        await context.send_activity(
            f"Saved. Conversation history will refresh: {submitted.refresh_cadence}."
        )
        return

    # Consent "Retry" button: re-run the message stashed when consent was required.
    _val = getattr(activity, "value", None)
    if isinstance(_val, dict) and _val.get("kind") == "consent_retry":
        user_text = _pending_consent.pop(conversation_id, "")
        if not user_text:
            await context.send_activity("Please resend your message.")
            return

    # RESET** wipes the agent's context window AND preferences (re-asks the poll).
    if prefs.is_reset_command(user_text):
        prefs.clear_history(conversation_id)
        prefs.forget_prefs(user_id)
        await context.send_activity(
            "Reset done: cleared the agent's conversation context and your "
            "preferences. Your visible chat is unchanged - use Teams' 'Remove "
            "chat history' to clear that too."
        )
        prefs.mark_asked(user_id)
        await context.send_activity(
            Activity(type=ActivityTypes.message, attachments=[prefs.build_prefs_card()])
        )
        return

    # First contact: ask the preference question once.
    if not prefs.was_asked(user_id):
        prefs.mark_asked(user_id)
        try:
            await context.send_activity(
                Activity(type=ActivityTypes.message, attachments=[prefs.build_prefs_card()])
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("could not send prefs card: %s", exc)

    original_user_text = user_text

    # Continuous loading indicator while the model + Starburst run.
    typing = asyncio.create_task(_keep_typing(context))
    consent_link = None
    reply_text = None
    try:
        # Ingest any Teams file attachments into the agent's session sandbox
        # ($HOME) so its code_interpreter_files tool can read them.
        session_id = _sessions.get(conversation_id)
        uploaded_names: list[str] = []
        if has_attachments:
            res = await sessions.ingest_activity_attachments(
                activity,
                _bridge.credential,
                config.FOUNDRY_PROJECT_ENDPOINT,
                config.TARGET_AGENT_NAME,
                session_id,
            )
            if res.session_id:
                session_id = res.session_id
                _sessions[conversation_id] = session_id
            uploaded_names = [u.filename for u in res.uploaded]
            if uploaded_names:
                await context.send_activity(
                    f"Received {len(uploaded_names)} file(s): {', '.join(uploaded_names)}"
                )
            elif res.errors:
                await context.send_activity(
                    "I couldn't read the attached file(s): " + "; ".join(res.errors)
                )
            if not user_text:
                user_text = (
                    "(The user uploaded file(s); acknowledge them and use them "
                    "for subsequent requests.)"
                )

        # Tell the agent which uploaded files are available (mounted names).
        if uploaded_names:
            hint = (
                "Files already uploaded to this session (mounted at /mnt/data/<name>; "
                "use code_interpreter_files with input_file=<name>): "
                + ", ".join(uploaded_names)
                + "\n\n"
            )
            user_text = hint + user_text

        # Snapshot session files before the turn to detect generated files after.
        files_before: set = set()
        if session_id:
            files_before = await sessions.list_session_files(
                _bridge.credential,
                config.FOUNDRY_PROJECT_ENDPOINT,
                config.TARGET_AGENT_NAME,
                session_id,
            )

        user_token = await _resolve_user_token(context)
        history = prefs.get_history(conversation_id, user_id)
        result = await _bridge.chat(
            config.TARGET_AGENT_NAME,
            user_text,
            user_token=user_token,
            history=history,
            session_id=session_id,
        )
        reply_text = result.text
        consent_link = result.consent_link
        if reply_text and not consent_link:
            prefs.append_history(conversation_id, original_user_text, reply_text)

        # Detect + surface files the agent generated this turn.
        if session_id and not consent_link:
            files_after = await sessions.list_session_files(
                _bridge.credential,
                config.FOUNDRY_PROJECT_ENDPOINT,
                config.TARGET_AGENT_NAME,
                session_id,
            )
            for name in sorted((files_after - files_before) - set(uploaded_names)):
                await _deliver_generated_file(context, session_id, name)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("[ERROR] turn failed: %s", exc, exc_info=True)
        reply_text = "Sorry, I hit a problem answering that."
    finally:
        typing.cancel()

    # Outbound delivery goes to the Bot Connector (serviceUrl). Guard it so a
    # transient delivery failure is logged instead of surfacing as a 500 on
    # the inbound webhook (which would make the Bot Connector retry).
    if consent_link:
        _pending_consent[conversation_id] = original_user_text
        try:
            await context.send_activity(
                Activity(type=ActivityTypes.message, attachments=[_build_consent_card(consent_link)])
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[ERROR] Could not send consent card: %s", exc)
        return

    try:
        await context.send_activity(reply_text or "(no response)")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[ERROR] Could not send reply: %s", exc)


@app.activity("invoke")
async def on_invoke(context, state):
    """Handle Teams file-consent Accept/Decline invokes."""
    if outfiles.is_file_consent_invoke(context.activity):
        try:
            await outfiles.handle_file_consent_invoke(context, _fetch_generated_bytes)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("[ERROR] file consent invoke failed: %s", exc, exc_info=True)
    try:
        from microsoft_agents.activity import InvokeResponse

        await context.send_activity(
            Activity(type=ActivityTypes.invoke_response, value=InvokeResponse(status=200))
        )
    except Exception:  # pylint: disable=broad-exception-caught
        pass


@app.activity("conversationUpdate")
async def on_members_added(context, state):
    """Welcome new members."""
    members = context.activity.members_added or []
    logger.info("[MEMBERS] CONVERSATION UPDATE | members_added=%d", len(members))

    for member in members:
        if member.id != context.activity.recipient.id:
            member_name = getattr(member, "name", "Guest")
            logger.info(
                "[MEMBER_ADDED] name=%s | id=%s",
                member_name,
                getattr(member, "id", "?"),
            )
            try:
                await context.send_activity("Welcome Buddy!")
                logger.info("[OK] Welcome message sent")
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("[ERROR] Could not send welcome: %s", exc)


@app.error
async def on_error(context, error):
    """Handle unhandled errors."""
    logger.error("[ERROR] HANDLER ERROR | error=%s", error, exc_info=True)
    await context.send_activity(f"Sorry, something went wrong: {error}")


if __name__ == "__main__":
    logger.info("Starting Activity-protocol agent (bring-your-own) ...")
    host.run()
