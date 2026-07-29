"""aiohttp entrypoint: Bot Framework Activity Protocol <-> Foundry Agents API.

Architecture:
  Teams -> Bot Service -> POST /api/messages on this container
    -> botbuilder validates the AAD JWT (audience = BOT_APP_ID)
    -> ActivityHandler.on_message_activity translates to Foundry call
    -> reply text is sent back via the same turn context (sync reply)

For runs that exceed Bot Framework's ~15s expectation we still reply on the
same turn — Foundry runs typically complete in <30s for Prompt Agents and the
Bot Service will hold the connection. Switch to a proactive-reply pattern if
runs routinely exceed 30s.
"""
from __future__ import annotations

import logging
import sys
import time
import urllib.parse
from types import SimpleNamespace

from aiohttp import web
from botbuilder.core import (
    CardFactory,
    MessageFactory,
    TurnContext,
)
from botbuilder.core.teams import TeamsActivityHandler
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity, Attachment, CardAction, HeroCard
from botbuilder.schema.teams import (
    FileConsentCard,
    FileConsentCardResponse,
    FileInfoCard,
)

from . import oauth
from .attachments import (
    download_container_file,
    ingest_activity_attachments,
    list_container_files,
    put_file_to_teams,
)
from .config import settings
from .foundry import FoundryClient
from .registry import AgentConfig, build_registry, normalize_bot_id
from .state import build_store, build_user_token_store

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
log = logging.getLogger("translator")


FILE_CONSENT_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
FILE_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.card.file.info"


class FoundryBot(TeamsActivityHandler):
    def __init__(self, foundry: FoundryClient, user_tokens, registry) -> None:
        self._foundry = foundry
        self._user_tokens = user_tokens
        self._registry = registry
        # conversation_id -> client-created container id (holds uploaded files
        # so Code Interpreter can read them across turns).
        self._containers: dict[str, str] = {}
        # conversation_id -> chat history [{role, content}] (this agent runs
        # store=False, so we carry state as a history array, not response ids).
        self._history: dict[str, list[dict]] = {}
        # conversation_id -> uploaded attachment manifest [{filename, path}].
        self._manifests: dict[str, list[dict]] = {}
        # cache generated-file bytes between the consent card and the accept
        # invoke (keyed by container_id:file_id); re-downloaded on cache miss.
        self._pending_files: dict[str, bytes] = {}

    async def on_message_activity(self, turn: TurnContext) -> None:
        text = (turn.activity.text or "").strip()
        has_attachments = bool(getattr(turn.activity, "attachments", None))
        if not text and not has_attachments:
            return
        conversation_id = turn.activity.conversation.id

        # Reset command: clear in-memory history, container, and attachments so
        # the user gets a clean conversation (Teams personal chat has no native
        # "new chat" button).
        if text.strip().lower() in {"reset", "/reset", "new chat", "/new", "clear", "/clear"}:
            self._history.pop(conversation_id, None)
            self._containers.pop(conversation_id, None)
            self._manifests.pop(conversation_id, None)
            await turn.send_activity(
                "\U0001F195 Started a fresh conversation — history, uploaded files, "
                "and container were cleared."
            )
            return

        bot_raw = turn.activity.recipient.id if turn.activity.recipient else ""
        cfg = self._registry.resolve(bot_raw)
        if cfg is None:
            log.warning("no agent registered for bot=%s", bot_raw)
            await turn.send_activity(
                "This agent isn't configured yet. Please contact the publisher."
            )
            return
        log.info(
            "conv=%s agent=%s mode=%s attachments=%s user=%r",
            conversation_id,
            cfg.foundry_agent_name,
            cfg.auth_mode,
            has_attachments,
            text[:80],
        )

        # ── Ingest any Teams file attachments into a Foundry container ──────
        container_id = self._containers.get(conversation_id)
        if has_attachments:
            result = await ingest_activity_attachments(
                turn.activity,
                self._foundry.credential,
                settings.foundry_project_endpoint,
                settings.containers_api_version,
                container_id,
            )
            if result.container_id:
                container_id = result.container_id
                self._containers[conversation_id] = container_id
            if result.uploaded:
                names = ", ".join(u.filename for u in result.uploaded)
                await turn.send_activity(
                    f"\U0001F4CE Received {len(result.uploaded)} file(s): {names}"
                )
                self._manifests.setdefault(conversation_id, []).extend(
                    {"filename": u.filename, "path": u.path} for u in result.uploaded
                )
            if result.errors:
                log.warning("attachment errors: %s", result.errors)
                if not result.uploaded:
                    await turn.send_activity(
                        "I couldn't read the attached file(s): "
                        + "; ".join(result.errors)
                    )
            # Attachment-only message (no caption): give the agent a neutral
            # prompt so it acknowledges the upload instead of receiving nothing.
            if not text:
                text = (
                    "(The user uploaded file(s); acknowledge them and use them "
                    "for subsequent requests.)"
                )

        user_token: str | None = None
        if cfg.auth_mode == "oauth":
            user_id = turn.activity.from_property.id
            token_key = f"{normalize_bot_id(bot_raw)}|{user_id}"
            user_token = await self._resolve_user_token(token_key, cfg)
            if not user_token:
                await self._send_signin_card(turn, token_key, cfg)
                return

        # Snapshot container files BEFORE the turn so we can detect new
        # assistant-generated files afterwards (the agent does not emit
        # container_file_citation annotations).
        files_before: set = set()
        if container_id:
            try:
                files_before = {
                    f.get("id")
                    for f in await list_container_files(
                        self._foundry.credential,
                        settings.foundry_project_endpoint,
                        container_id,
                    )
                }
            except Exception:  # noqa: BLE001
                log.warning("pre-turn container listing failed", exc_info=True)

        history = self._history.get(conversation_id, [])
        try:
            reply = await self._foundry.chat(
                cfg.foundry_agent_name,
                conversation_id,
                text,
                user_token,
                container_id=container_id,
                history=history,
                attachments_manifest=self._manifests.get(conversation_id),
            )
        except Exception:  # noqa: BLE001
            log.exception("Foundry call failed")
            await turn.send_activity(
                "Sorry, the agent backend hit an error. Please try again."
            )
            return

        # Foundry asked the user to authorize a tool (OAuth Identity Passthrough):
        # surface its consent_link as an "Open consent" card. After the user
        # authorizes, they re-send their message and the tool runs as them.
        if reply.consent_link:
            card = HeroCard(
                title="Sign in required",
                text=(
                    "This agent needs your permission to access data on your "
                    "behalf. Tap to authorize, then send your message again."
                ),
                buttons=[
                    CardAction(type="openUrl", title="Open consent", value=reply.consent_link)
                ],
            )
            await turn.send_activity(
                MessageFactory.attachment(CardFactory.hero_card(card))
            )
            return

        await turn.send_activity(reply.text or "(empty agent reply)")

        # Advance history on a real text turn.
        if reply.text:
            self._history.setdefault(conversation_id, []).extend(
                [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": reply.text},
                ]
            )

        # Detect newly created assistant files in the container and deliver them
        # via the Teams file-consent flow (personal 1:1 scope only).
        if container_id:
            await self._deliver_new_files(turn, container_id, files_before)

    async def _deliver_new_files(
        self, turn: TurnContext, container_id: str, files_before: set
    ) -> None:
        try:
            files_after = await list_container_files(
                self._foundry.credential,
                settings.foundry_project_endpoint,
                container_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("post-turn container listing failed", exc_info=True)
            return
        new_assistant = [
            f
            for f in files_after
            if f.get("id") not in files_before
            and (f.get("source") == "assistant")
        ]
        if not new_assistant:
            return
        conv_type = getattr(turn.activity.conversation, "conversation_type", None)
        if conv_type not in (None, "personal"):
            await turn.send_activity(
                "I generated a file, but file delivery is only supported in a "
                "1:1 chat with me."
            )
            return
        for f in new_assistant:
            path = f.get("path") or f.get("id")
            filename = str(path).rsplit("/", 1)[-1]
            await self._offer_generated_file(
                turn, container_id, f.get("id"), filename
            )

    async def _offer_generated_file(
        self, turn: TurnContext, container_id: str, file_id: str, filename: str
    ) -> None:
        """Download a generated container file and offer it via a consent card."""
        try:
            content = await download_container_file(
                self._foundry.credential,
                settings.foundry_project_endpoint,
                container_id,
                file_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to download generated file %s", filename)
            await turn.send_activity(
                f"I generated {filename} but couldn't retrieve it: {exc}"
            )
            return

        key = f"{container_id}:{file_id}"
        self._pending_files[key] = content
        consent = FileConsentCard(
            description=f"{filename} generated by the agent",
            size_in_bytes=len(content),
            accept_context={
                "container_id": container_id,
                "file_id": file_id,
                "filename": filename,
            },
            decline_context={"filename": filename},
        )
        attachment = Attachment(
            content=consent.serialize(),
            content_type=FILE_CONSENT_CONTENT_TYPE,
            name=filename,
        )
        await turn.send_activity(MessageFactory.attachment(attachment))

    async def on_teams_file_consent_accept(
        self, turn: TurnContext, file_consent_card_response: FileConsentCardResponse
    ) -> None:
        ctx = file_consent_card_response.context or {}
        upload = file_consent_card_response.upload_info
        filename = (ctx.get("filename") if isinstance(ctx, dict) else None) or (
            upload.name if upload else "file"
        )
        try:
            key = f"{ctx.get('container_id')}:{ctx.get('file_id')}"
            content = self._pending_files.pop(key, None)
            if content is None:
                content = await download_container_file(
                    self._foundry.credential,
                    settings.foundry_project_endpoint,
                    ctx.get("container_id"),
                    ctx.get("file_id"),
                )
            await put_file_to_teams(upload.upload_url, content)
        except Exception as exc:  # noqa: BLE001
            log.exception("file consent accept failed for %s", filename)
            await turn.send_activity(f"Sorry, uploading {filename} failed: {exc}")
            return

        info = FileInfoCard(unique_id=upload.unique_id, file_type=upload.file_type)
        attachment = Attachment(
            content=info.serialize(),
            content_type=FILE_INFO_CONTENT_TYPE,
            name=upload.name,
            content_url=upload.content_url,
        )
        await turn.send_activity(MessageFactory.attachment(attachment))

    async def on_teams_file_consent_decline(
        self, turn: TurnContext, file_consent_card_response: FileConsentCardResponse
    ) -> None:
        ctx = file_consent_card_response.context or {}
        filename = ctx.get("filename") if isinstance(ctx, dict) else None
        self._pending_files.pop(
            f"{ctx.get('container_id')}:{ctx.get('file_id')}", None
        )
        await turn.send_activity(
            f"No problem — I won't send {filename or 'the file'}."
        )


    async def _resolve_user_token(self, token_key: str, cfg: AgentConfig) -> str | None:
        """Return a valid user access token, refreshing if near expiry; else None."""
        data = await self._user_tokens.get(token_key)
        if not data or not data.get("access_token"):
            return None
        if float(data.get("expires_at", 0)) - time.time() < 120:
            rt = data.get("refresh_token")
            if not rt:
                return None
            tok = await oauth.refresh_token(cfg.oauth, rt)
            if "access_token" not in tok:
                log.warning("token refresh failed: %s", tok.get("error"))
                return None
            await self._user_tokens.put(token_key, _token_to_record(tok))
            return tok["access_token"]
        return data["access_token"]

    async def _send_signin_card(
        self, turn: TurnContext, token_key: str, cfg: AgentConfig
    ) -> None:
        base = cfg.oauth.redirect_base_url.rstrip("/")
        start_url = (
            f"{base}/api/oauth/start?state={urllib.parse.quote(token_key, safe='')}"
        )
        card = HeroCard(
            title="Sign in required",
            text="Sign in to authorize this agent to access data on your behalf.",
            buttons=[CardAction(type="openUrl", title="Sign in", value=start_url)],
        )
        await turn.send_activity(MessageFactory.attachment(CardFactory.hero_card(card)))


def _token_to_record(tok: dict) -> dict:
    expires_in = float(tok.get("expires_in", 3600))
    return {
        "access_token": tok.get("access_token", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "expires_at": time.time() + expires_in,
    }


def build_app() -> web.Application:
    registry = build_registry()

    async def on_error(context: TurnContext, error: Exception) -> None:
        log.exception("Adapter error: %s", error)
        await context.send_activity("The bot encountered an internal error.")

    # One CloudAdapter per bot, built lazily from the registry so a single
    # container validates/signs each bot's traffic with its own credentials.
    adapters: dict[str, CloudAdapter] = {}

    def adapter_for(cfg: AgentConfig) -> CloudAdapter:
        ad = adapters.get(cfg.key)
        if ad is None:
            auth_configuration = SimpleNamespace(
                APP_ID=cfg.bot_app_id,
                APP_PASSWORD=cfg.bot_app_password,
                APP_TYPE=cfg.bot_app_type or "SingleTenant",
                APP_TENANTID=cfg.bot_app_tenant_id,
            )
            auth = ConfigurationBotFrameworkAuthentication(
                configuration=auth_configuration
            )
            ad = CloudAdapter(auth)
            ad.on_turn_error = on_error
            adapters[cfg.key] = ad
        return ad

    store = build_store()
    user_tokens = build_user_token_store()
    foundry = FoundryClient(store)
    bot = FoundryBot(foundry, user_tokens, registry)

    async def messages(req: web.Request) -> web.Response:
        if "application/json" not in req.headers.get("Content-Type", ""):
            return web.Response(status=415)
        body = await req.json()
        activity = Activity().deserialize(body)
        bot_raw = activity.recipient.id if activity.recipient else ""
        cfg = registry.resolve(bot_raw)
        if cfg is None:
            log.warning("no agent registered for bot=%s", bot_raw)
            return web.Response(status=404)
        auth_header = req.headers.get("Authorization", "")
        try:
            await adapter_for(cfg).process_activity(auth_header, activity, bot.on_turn)
            return web.Response(status=200)
        except Exception:  # noqa: BLE001
            log.exception("process_activity failed")
            return web.Response(status=500)

    async def health(_req: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "agents": [a.foundry_agent_name for a in registry.all()]}
        )

    def _cfg_from_state(state: str) -> AgentConfig | None:
        bot_id = state.split("|", 1)[0]
        return registry.resolve(bot_id)

    async def oauth_start(req: web.Request) -> web.Response:
        state = req.query.get("state", "")
        cfg = _cfg_from_state(state)
        if cfg is None or cfg.oauth is None:
            return web.Response(text="Unknown or non-OAuth agent.", status=400)
        return web.HTTPFound(oauth.build_authorize_url(cfg.oauth, state))

    async def oauth_callback(req: web.Request) -> web.Response:
        if req.query.get("error"):
            return web.Response(
                text=(
                    f"Sign-in failed: {req.query.get('error')} — "
                    f"{req.query.get('error_description', '')}"
                ),
                content_type="text/html",
                status=400,
            )
        code = req.query.get("code", "")
        state = req.query.get("state", "")
        if not code or not state:
            return web.Response(
                text="Missing code or state.", content_type="text/html", status=400
            )
        cfg = _cfg_from_state(state)
        if cfg is None or cfg.oauth is None:
            return web.Response(text="Unknown or non-OAuth agent.", status=400)
        tok = await oauth.exchange_code(cfg.oauth, code)
        if "access_token" not in tok:
            return web.Response(
                text=(
                    f"Token exchange failed: {tok.get('error')} — "
                    f"{tok.get('error_description', '')}"
                ),
                content_type="text/html",
                status=400,
            )
        await user_tokens.put(urllib.parse.unquote(state), _token_to_record(tok))
        return web.Response(
            text=(
                "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
                "<h2>Signed in!</h2><p>You can close this window and return to Teams.</p>"
                "<script>setTimeout(function(){window.close();},2000);</script>"
                "</body></html>"
            ),
            content_type="text/html",
        )

    async def on_shutdown(_app: web.Application) -> None:
        await foundry.close()
        await store.close()
        await user_tokens.close()

    app = web.Application()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/api/oauth/start", oauth_start)
    app.router.add_get("/api/oauth/callback", oauth_callback)
    app.router.add_get("/health", health)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    app = build_app()
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
