"""orchestrator: the single point of contact for MQTT, PostgREST, and Gemini.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.

Every inbound message, whatever it contains (text, an image, a document, a
pasted URL), goes straight into `llm.client.run_agent_loop` (`shared.gemini_client`)
with the full tool surface from `actions.py`. Orchestrator itself never decides
what a message means — it only builds the user message, runs the loop,
executes whatever the model calls, and persists the result (CLAUDE.md section
10, "the model drives via tool-use"). Two things can make the loop pause
without executing a tool (`actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES`):

- `extract_device_data`: requires an HTTP round trip to doc-ingestion-worker
  that can't be awaited inline — kicked off here, resumed when that service
  calls back (`handle_doc_ingestion_result`).
- `create_device` / `update_device` / `retire_device`: writes to the
  inventory, gated on the user's explicit approval (Human-in-the-Loop) —
  sent out as a channel message with `actions` (Aprobar/Rechazar buttons) —
  or, on channels without real buttons, a plain sí/no text reply works too
  (`security_guard.parse_confirmation_text`) — resumed when the decision
  comes back in (`_resolve_pending_confirmation`).

Runs two independent things side by side: the MQTT connection (consuming
`home/inbound/+/+`, same as before) and a small internal HTTP API — reachable
only on the `barbaraServices` Docker network, no host port published, no auth
(same trusted-LAN reasoning as PostgREST without JWT and web-adapter without
a login) — that doc-ingestion-worker calls instead of talking to MQTT/PostgREST
itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from shared.gemini_client import AgentTurnResult
from shared.internal_client import InternalApiClient
from shared.message import DocIngestionRequest, DocIngestionResult, MessageType, NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from . import actions, llm, security_guard
from .config import SERVICE_NAME, appconfig, doc_ingestion_worker_secrets, mqtt_secrets, postgrest_secrets, system
from .conversation import clear_keys, get_conversation_by_id, get_or_create_conversation, update_state
from .messaging import reply, reply_raw

logger = logging.getLogger("orchestrator")

_mqtt = ManagedMqttConnection("orchestrator")
_doc_ingestion_client = InternalApiClient(doc_ingestion_worker_secrets.url, "orchestrator")

_PAUSE_TOOL_NAMES = actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES


async def _run_agent_loop(pg: PostgrestClient, history: list[dict[str, Any]]) -> AgentTurnResult:
    return await llm.client.run_agent_loop(
        llm.SYSTEM_PROMPT,
        actions.TOOL_SCHEMAS,
        actions.make_executor(pg),
        history,
        max_tokens=llm.max_tokens(),
        async_tool_names=_PAUSE_TOOL_NAMES,
        max_iterations_fallback=llm.MAX_ITERATIONS_FALLBACK,
        web_search=appconfig.get("webSearchEnabled", True),
    )


async def _resume_agent_loop(
    pg: PostgrestClient, history: list[dict[str, Any]], resolved_tool_results: dict[str, Any]
) -> AgentTurnResult:
    return await llm.client.resume_agent_loop(
        llm.SYSTEM_PROMPT,
        actions.TOOL_SCHEMAS,
        actions.make_executor(pg),
        history,
        resolved_tool_results=resolved_tool_results,
        max_tokens=llm.max_tokens(),
        async_tool_names=_PAUSE_TOOL_NAMES,
        max_iterations_fallback=llm.MAX_ITERATIONS_FALLBACK,
        web_search=appconfig.get("webSearchEnabled", True),
    )


async def handle_inbound(pg: PostgrestClient, mqtt: ManagedMqttConnection, payload: bytes) -> None:
    msg = NormalizedMessage.model_validate_json(payload)
    conversation = await get_or_create_conversation(pg, msg.channel, msg.conversation_id)
    state = conversation["state"]

    if msg.type == MessageType.CALLBACK:
        # A real button press (Telegram inline keyboard) — content is exactly "approve"/"reject".
        await _resolve_pending_confirmation(pg, mqtt, conversation, msg, msg.content == "approve")
        return

    if state.get("pending_agent_turn"):
        if state.get("pending_confirmation"):
            # Text-only channels (web-adapter) or a Telegram user who typed instead of
            # tapping a button — accept a plain sí/no reply as the same decision.
            decision = security_guard.parse_confirmation_text(msg.content)
            if decision is not None:
                await _resolve_pending_confirmation(pg, mqtt, conversation, msg, decision)
                return
            await reply(mqtt, msg, "Todavía espero tu confirmación (responde 'sí'/'no', o Aprobar/Rechazar) sobre la acción anterior.")
        else:
            await reply(mqtt, msg, "Sigo analizando la foto anterior, dame un momento...")
        return

    history: list[dict[str, Any]] = state.get("history", [])
    user_message = await llm.client.build_user_message(msg.content, msg.attachments)
    history = [*history, user_message]

    result = await _run_agent_loop(pg, history)

    if result.done:
        await update_state(pg, conversation["id"], {**state, "history": result.messages})
        await reply(mqtt, msg, result.final_text or "")
        return

    tool_use = llm.client.find_tool_use(result.messages)
    if tool_use is None:
        logger.error("Agent loop paused with no pending tool_use — conversation %s", conversation["id"])
        await update_state(pg, conversation["id"], {**state, "history": result.messages})
        await reply(mqtt, msg, "Algo ha ido mal — inténtalo de nuevo.")
        return

    if tool_use["name"] == "extract_device_data":
        await _kick_off_extraction(pg, mqtt, conversation, msg, result, tool_use)
    elif tool_use["name"] in actions.CONFIRM_TOOL_NAMES:
        await _kick_off_confirmation(pg, mqtt, conversation, msg, result, tool_use)
    else:
        logger.error("Agent loop paused on unexpected tool %r — conversation %s", tool_use["name"], conversation["id"])
        await update_state(pg, conversation["id"], {**state, "history": result.messages})
        await reply(mqtt, msg, "Algo ha ido mal — inténtalo de nuevo.")


async def _kick_off_extraction(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Fires the actual `/extract` request for the `extract_device_data` tool
    call the loop just paused on. Doesn't touch `conversation.state` at all
    on failure — an unresolvable pending turn would just hang forever waiting
    for a callback that's never coming, so the safest thing is to leave the
    conversation exactly as it was before this message."""
    index = tool_use["input"].get("attachment_index", 0)
    if not msg.attachments or index >= len(msg.attachments):
        await reply(mqtt, msg, "No he recibido ninguna foto que analizar — vuelve a intentarlo.")
        return

    request = DocIngestionRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=msg.conversation_id,
        channel=msg.channel,
        user_id=msg.user_id,
        attachment_url=msg.attachments[index].url_or_data,
    )
    # Fire-and-forget: doc-ingestion-worker accepts the job and replies later
    # via POST /internal/doc-ingestion/result (see handle_doc_ingestion_result).
    response = await _doc_ingestion_client.post("/extract", json=request.model_dump())
    if response is None:
        # InternalApiClient already logged the failure after its own retries.
        await reply(mqtt, msg, "No puedo procesar la foto ahora mismo — inténtalo de nuevo en un momento.")
        return

    # `pending_agent_turn` carries the pending tool_use_id itself (not just a
    # boolean) — resume_agent_loop resolves `resolved_tool_results` by that id
    # once the callback below has the extraction result.
    new_state = {**conversation["state"], "history": result.messages, "pending_agent_turn": tool_use["id"]}
    await update_state(pg, conversation["id"], new_state)
    await reply(mqtt, msg, result.final_text or "Recibido. Dame un momento para analizar la foto...")


async def _kick_off_confirmation(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Pauses the loop on a write tool (`actions.CONFIRM_TOOL_NAMES`) and asks
    the user to approve/reject it via the channel (Human-in-the-Loop, see
    `security_guard.py`) before it's actually dispatched. Only one action can
    be pending per conversation at a time — same constraint
    `extract_device_data` already has — so the button press just needs to
    say "approve"/"reject", no correlation id."""
    new_state = {
        **conversation["state"],
        "history": result.messages,
        "pending_agent_turn": tool_use["id"],
        "pending_confirmation": {"tool_name": tool_use["name"], "tool_input": tool_use["input"]},
    }
    await update_state(pg, conversation["id"], new_state)
    prompt = security_guard.confirmation_prompt(tool_use["name"], result.final_text)
    await reply(mqtt, msg, prompt, actions=security_guard.APPROVE_ACTIONS)


async def _resolve_pending_confirmation(
    pg: PostgrestClient, mqtt: ManagedMqttConnection, conversation: dict[str, Any], msg: NormalizedMessage, approved: bool
) -> None:
    state = conversation["state"]
    pending = state.get("pending_confirmation")
    pending_tool_use_id = state.get("pending_agent_turn")
    if not pending or not pending_tool_use_id:
        await reply(mqtt, msg, "No hay ninguna acción pendiente de confirmar.")
        return

    tool_result = await security_guard.resolve(pg, pending, approved)
    agent_result = await _resume_agent_loop(pg, state.get("history", []), {pending_tool_use_id: tool_result})
    await _finish_paused_turn(pg, mqtt, conversation, msg.channel, msg.user_id, msg.conversation_id, agent_result)


async def _finish_paused_turn(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    agent_result: AgentTurnResult,
) -> None:
    """Shared tail end for both resume paths (doc-ingestion callback and
    approval callback): clear the pending state, persist history, reply."""
    reply_text = agent_result.final_text or ""
    if not agent_result.done:
        # The model asked for another paused tool in the same resumed turn
        # (e.g. two photos, or one action right after another) — not
        # supported yet, degrade gracefully instead of leaving the
        # conversation stuck forever (CLAUDE.md known gap #7).
        logger.warning("Agent loop paused again on resume — conversation %s (not supported, degrading)", conversation["id"])
        reply_text = "He completado ese paso, pero necesito que me pidas el siguiente por separado."

    new_state = clear_keys(
        {**conversation["state"], "history": agent_result.messages}, "pending_agent_turn", "pending_confirmation"
    )
    await update_state(pg, conversation["id"], new_state)
    await reply_raw(mqtt, channel, user_id, channel_conversation_id, reply_text)


# --- Internal HTTP API (doc-ingestion-worker) ------------------------------


async def handle_doc_ingestion_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = DocIngestionResult.model_validate(body)
    conversation = await get_conversation_by_id(pg, result.conversation_id)
    pending_tool_use_id = conversation["state"]["pending_agent_turn"]

    tool_result: Any = result.draft_device if (result.success and result.draft_device) else {
        "success": False,
        "error": result.error or "unknown error",
    }

    agent_result = await _resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await _finish_paused_turn(
        pg, _mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result
    )

    return web.json_response({"ok": True})


def build_app(pg: PostgrestClient) -> web.Application:
    app = web.Application()
    app["pg"] = pg
    app.router.add_post("/internal/doc-ingestion/result", handle_doc_ingestion_result)
    return app


async def _run() -> None:
    logger.info("orchestrator starting up (PostgREST: %s, engine: gemini)", postgrest_secrets.url)
    pg = PostgrestClient(postgrest_secrets.url)

    async def on_mqtt_connect(client) -> None:
        await _mqtt.consume(client, "home/inbound/+/+", lambda message: handle_inbound(pg, _mqtt, message.payload))

    mqtt_task = asyncio.create_task(maintain_mqtt_connection(mqtt_secrets, system, on_mqtt_connect))
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))

    app = build_app(pg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=appconfig.get("port", 8080))
    await site.start()
    logger.info("orchestrator internal API listening on :%s", appconfig.get("port", 8080))

    try:
        await asyncio.Event().wait()  # keeps the process alive
    finally:
        mqtt_task.cancel()
        config_task.cancel()
        await runner.cleanup()
        await pg.aclose()
        await _doc_ingestion_client.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
