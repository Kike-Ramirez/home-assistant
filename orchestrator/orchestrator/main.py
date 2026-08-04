"""orchestrator: the single point of contact for MQTT, PostgREST, and Gemini.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.

Every inbound message, whatever it contains (text, an image, a document, a
pasted URL), goes straight into `llm.client.run_agent_loop` (`shared.gemini_client`)
with the full tool surface from `actions.py`. Orchestrator itself never decides
what a message means — it only builds the user message, runs the loop,
executes whatever the model calls, and persists the result (CLAUDE.md section
10, "the model drives via tool-use"). Three things can make the loop pause
without executing a tool (`actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES`)
— see `pauses.py` for how each of these is actually handled:

- `extract_device_data`: requires an HTTP round trip to doc-ingestion-worker
  that can't be awaited inline — kicked off in `pauses.py`, resumed when that
  service calls back (`callbacks.py::handle_doc_ingestion_result`).
- `generate_document`: same shape, an HTTP round trip to doc-generation-worker
  to render a file the model already wrote the content for — resumed when
  that service calls back (`callbacks.py::handle_doc_generation_result`), and
  the rendered file is delivered to the user as an attachment on that same
  reply.
- `generate_image`: if `device_id` is given, first checks for a photo
  already attached to that device (`registry.get_latest_device_photo`) and,
  if one's found and still fetchable, resolves immediately with no worker
  round trip at all. Otherwise same shape as the other two: an HTTP round
  trip to image-generation-worker (real image search first, Gemini
  generation as a fallback) — resumed when that service calls back
  (`callbacks.py::handle_image_generation_result`). Either way, delivered as
  a Telegram photo (not a generic file) via `AttachmentKind.IMAGE`.
- `create_device` / `update_device` / `retire_device`: writes to the
  inventory, gated on the user's explicit approval (Human-in-the-Loop) —
  sent out as a channel message with `actions` (Aprobar/Rechazar buttons) —
  or, on channels without real buttons, a plain sí/no text reply works too
  (`security_guard.parse_confirmation_text`) — resumed when the decision
  comes back in (`pauses.py::resolve_pending_confirmation`).

A single inbound message can carry several attachments (e.g. a Telegram
album of photos) — `conversation.state.pending_attachments` keeps the
original message's attachments available across however many pause/resume
round trips it takes for the model to work through all of them (one at a
time: `pauses.py::finish_paused_turn` re-enters `kick_off_pending_tool` on
every resume, instead of giving up after the first one — see CLAUDE.md's
former known-gap #7).

Runs two independent things side by side: the MQTT connection (consuming
`home/inbound/+/+`, same as before) and a small internal HTTP API — reachable
only on the `barbaraServices` Docker network, no host port published, no auth
(same trusted-LAN reasoning as PostgREST without JWT and web-adapter without
a login) — that doc-ingestion-worker, doc-generation-worker, and
image-generation-worker call instead of talking to MQTT/PostgREST themselves
(`callbacks.py`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from shared.message import MessageType, NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from . import llm, security_guard
from .callbacks import build_app
from .config import SERVICE_NAME, appconfig, mqtt_secrets, postgrest_secrets, system, welcome_secrets
from .conversation import get_or_create_conversation, update_state
from .messaging import reply, reply_raw
from .pauses import kick_off_pending_tool, resolve_pending_confirmation
from .runtime import PAUSE_TOOL_NAMES, aclose_clients, mqtt, run_agent_loop

logger = logging.getLogger("orchestrator")

# Sent once, the first time MQTT connects after startup (see _run) — a fixed
# string, not Claude-generated, so it doesn't depend on Gemini being reachable
# yet at boot and doesn't cost a call for something said the same way every time.
WELCOME_MESSAGE = (
    "¡Hola! 👋 Soy tu asistente para los aparatos de casa, ya listo para ayudarte.\n\n"
    "Puedo:\n"
    "• Dar de alta un dispositivo nuevo — mándame una foto de la etiqueta o el manual.\n"
    "• Ayudarte a resolver una avería o duda técnica de algo que ya tienes.\n"
    "• Prepararte un mini-curso (con quiz) sobre lo que quieras aprender.\n"
    "• Recomendarte un recambio o una compra nueva compatible con lo que ya tienes en casa.\n\n"
    "Cuéntame qué necesitas cuando quieras. 🙂"
)


async def handle_inbound(pg: PostgrestClient, mqtt: ManagedMqttConnection, payload: bytes) -> None:
    msg = NormalizedMessage.model_validate_json(payload)
    conversation = await get_or_create_conversation(pg, msg.channel, msg.conversation_id)
    state = conversation["state"]

    if msg.type == MessageType.CALLBACK:
        # A real button press (Telegram inline keyboard) — content is exactly "approve"/"reject".
        await resolve_pending_confirmation(pg, mqtt, conversation, msg, msg.content == "approve")
        return

    if state.get("pending_agent_turn"):
        if state.get("pending_confirmation"):
            # Text-only channels (web-adapter) or a Telegram user who typed instead of
            # tapping a button — accept a plain sí/no reply as the same decision.
            decision = security_guard.parse_confirmation_text(msg.content)
            if decision is not None:
                await resolve_pending_confirmation(pg, mqtt, conversation, msg, decision)
                return
            await reply(mqtt, msg, "Todavía espero tu confirmación (responde 'sí'/'no', o Aprobar/Rechazar) sobre la acción anterior.")
        else:
            await reply(mqtt, msg, "Sigo con lo anterior, dame un momento...")
        return

    history: list[dict[str, Any]] = state.get("history", [])
    user_message = await llm.client.build_user_message(msg.content, msg.attachments)
    history = [*history, user_message]

    result = await run_agent_loop(pg, history)

    if result.done:
        await update_state(pg, conversation, lambda s: {**s, "history": result.messages})
        await reply(mqtt, msg, result.final_text or llm.DEFAULT_DONE_FALLBACK)
        return

    tool_use = llm.client.find_tool_use(result.messages, names=PAUSE_TOOL_NAMES)
    if tool_use is None:
        logger.error(
            "Agent loop paused with no pending tool_use — conversation %s. Likely cause: run_agent_loop "
            "returned done=False without a function_call in the last part — check GeminiClient._loop in "
            "shared/shared/gemini_client.py.",
            conversation["id"],
        )
        await update_state(pg, conversation, lambda s: {**s, "history": result.messages})
        await reply(mqtt, msg, "Algo ha ido mal — inténtalo de nuevo.")
        return

    await kick_off_pending_tool(
        pg, mqtt, conversation, msg.channel, msg.user_id, msg.conversation_id, msg.attachments, result, tool_use
    )


async def _send_welcome_message() -> None:
    if not welcome_secrets.admin_chat_id:
        logger.warning(
            "TELEGRAM_ADMIN_CHAT_ID isn't set — skipping the startup welcome message. Suggested fix: add "
            "that variable to barbarasecrets.env with your own Telegram chat id."
        )
        return
    await reply_raw(mqtt, "telegram", welcome_secrets.admin_chat_id, welcome_secrets.admin_chat_id, WELCOME_MESSAGE)


async def _run() -> None:
    pg = PostgrestClient(postgrest_secrets.url)
    _ready_logged = False
    _http_ready = asyncio.Event()

    async def on_mqtt_connect(client) -> None:
        nonlocal _ready_logged
        mqtt.bind(client)
        if not _ready_logged:
            _ready_logged = True
            await _http_ready.wait()  # so "ready" is only logged once BOTH MQTT and the internal API are up
            logger.info("orchestrator ready: MQTT connected, PostgREST reachable, Gemini engine configured.")
            await _send_welcome_message()
        await mqtt.consume(client, "home/inbound/+/+", lambda message: handle_inbound(pg, mqtt, message.payload))

    mqtt_task = asyncio.create_task(maintain_mqtt_connection(mqtt_secrets, system, on_mqtt_connect))
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))

    app = build_app(pg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=appconfig.get("port", 8080))
    await site.start()
    _http_ready.set()

    try:
        await asyncio.Event().wait()  # keeps the process alive
    finally:
        mqtt_task.cancel()
        config_task.cancel()
        await runner.cleanup()
        await pg.aclose()
        await aclose_clients()


if __name__ == "__main__":
    asyncio.run(_run())
