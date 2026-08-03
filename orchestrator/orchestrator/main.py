"""orchestrator: the single point of contact for MQTT, PostgREST, and the LLM engine.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.

Every inbound message, whatever it contains (text, an image, a document, a
pasted URL), goes straight into `claude_client.engine.run_agent_loop` (see
`shared/shared/engines/` — pluggable, Gemini by default) with the full tool
surface from `tools.py`. Orchestrator itself never decides what a message
means — it only builds the user message, runs the loop, executes whatever
the model calls, and persists the result (CLAUDE.md section 10, "the model
drives via tool-use"). The one exception is `extract_device_data`,
the single async tool: it's kicked off here (not inside the loop) because it
requires an HTTP round trip to doc-ingestion-worker that can't be awaited
inline, and resumed here when that service calls back.

Runs two independent things side by side: the MQTT connection (consuming
`home/inbound/+/+`, same as before) and a small internal HTTP API — reachable
only on the `barbaraServices` Docker network, no host port published, no auth
(same trusted-LAN reasoning as PostgREST without JWT and web-adapter without
a login) — that doc-ingestion-worker and notifier-scheduler call instead of
talking to MQTT/PostgREST themselves.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web
from shared.engines import AgentTurnResult
from shared.internal_client import InternalApiClient
from shared.message import DocIngestionRequest, DocIngestionResult, NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from . import claude_client, tools
from .config import SERVICE_NAME, appconfig, doc_ingestion_worker_secrets, mqtt_secrets, postgrest_secrets, system
from .conversation import clear_keys, get_conversation_by_id, get_or_create_conversation, update_state
from .messaging import reply, reply_raw
from .reminders import check_reminders

logger = logging.getLogger("orchestrator")

_mqtt = ManagedMqttConnection("orchestrator")
_doc_ingestion_client = InternalApiClient(doc_ingestion_worker_secrets.url, "orchestrator")


async def handle_inbound(pg: PostgrestClient, mqtt: ManagedMqttConnection, payload: bytes) -> None:
    msg = NormalizedMessage.model_validate_json(payload)
    conversation = await get_or_create_conversation(pg, msg.channel, msg.conversation_id)

    if conversation["state"].get("pending_agent_turn"):
        await reply(mqtt, msg, "Sigo analizando la foto anterior, dame un momento...")
        return

    history: list[dict[str, Any]] = conversation["state"].get("history", [])
    user_message = await claude_client.engine.build_user_message(msg.content, msg.attachments)
    history = [*history, user_message]

    ctx = tools.ToolContext(channel=msg.channel, channel_user_id=msg.user_id)
    result = await claude_client.engine.run_agent_loop(
        claude_client.SYSTEM_PROMPT,
        tools.TOOL_SCHEMAS,
        tools.make_executor(pg, ctx),
        history,
        max_tokens=claude_client.max_tokens(),
        async_tool_names=tools.ASYNC_TOOL_NAMES,
        max_iterations_fallback=claude_client.MAX_ITERATIONS_FALLBACK,
        web_search=appconfig.get("webSearchEnabled", True),
    )

    if result.done:
        await update_state(pg, conversation["id"], {**conversation["state"], "history": result.messages})
        await reply(mqtt, msg, result.final_text or "")
        return

    await _kick_off_extraction(pg, mqtt, conversation, msg, result)


async def _kick_off_extraction(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
    result: AgentTurnResult,
) -> None:
    """Fires the actual `/extract` request for the `extract_device_data` tool
    call the loop just paused on. Doesn't touch `conversation.state` at all
    on failure — an unresolvable pending turn would just hang forever waiting
    for a callback that's never coming, so the safest thing is to leave the
    conversation exactly as it was before this message."""
    tool_use = claude_client.engine.find_tool_use(result.messages, name="extract_device_data")
    if tool_use is None:
        logger.error("Agent loop paused with no extract_device_data tool_use — conversation %s", conversation["id"])
        await reply(mqtt, msg, "Algo ha ido mal analizando la foto — inténtalo de nuevo.")
        return

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


# --- Internal HTTP API (doc-ingestion-worker, notifier-scheduler) ----------


async def handle_doc_ingestion_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = DocIngestionResult.model_validate(body)
    conversation = await get_conversation_by_id(pg, result.conversation_id)
    history: list[dict[str, Any]] = conversation["state"].get("history", [])
    pending_tool_use_id = conversation["state"]["pending_agent_turn"]

    tool_result: Any = result.draft_device if (result.success and result.draft_device) else {
        "success": False,
        "error": result.error or "unknown error",
    }

    ctx = tools.ToolContext(channel=result.channel, channel_user_id=result.user_id)
    agent_result = await claude_client.engine.resume_agent_loop(
        claude_client.SYSTEM_PROMPT,
        tools.TOOL_SCHEMAS,
        tools.make_executor(pg, ctx),
        history,
        resolved_tool_results={pending_tool_use_id: tool_result},
        max_tokens=claude_client.max_tokens(),
        async_tool_names=tools.ASYNC_TOOL_NAMES,
        max_iterations_fallback=claude_client.MAX_ITERATIONS_FALLBACK,
        web_search=appconfig.get("webSearchEnabled", True),
    )

    reply_text = agent_result.final_text or ""
    if not agent_result.done:
        # Claude asked for another extraction in the same resumed turn (e.g. a
        # message with more than one photo) — not supported yet, degrade
        # gracefully instead of leaving the conversation stuck forever.
        logger.warning("Agent loop paused again on resume — conversation %s (not supported, degrading)", conversation["id"])
        reply_text = "He podido analizar la foto, pero necesito que me envíes las demás una a una."

    new_state = clear_keys({**conversation["state"], "history": agent_result.messages}, "pending_agent_turn")
    await update_state(pg, conversation["id"], new_state)
    await reply_raw(_mqtt, result.channel, result.user_id, result.channel_conversation_id, reply_text)

    return web.json_response({"ok": True})


async def handle_reminders_check(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    processed = await check_reminders(pg, _mqtt)
    return web.json_response({"processed": processed})


def build_app(pg: PostgrestClient) -> web.Application:
    app = web.Application()
    app["pg"] = pg
    app.router.add_post("/internal/doc-ingestion/result", handle_doc_ingestion_result)
    app.router.add_post("/internal/reminders/check", handle_reminders_check)
    return app


async def _run() -> None:
    logger.info(
        "orchestrator starting up (PostgREST: %s, engine: %s)",
        postgrest_secrets.url,
        claude_client.ENGINE_NAME,
    )
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
