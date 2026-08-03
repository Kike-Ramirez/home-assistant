"""orchestrator: the single point of contact for MQTT, PostgREST, and Claude.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.

Runs two independent things side by side: the MQTT connection (consuming
`home/inbound/+/+`, same as before) and a small internal HTTP API — reachable
only on the `barbaraServices` Docker network, no host port published, no auth
(same trusted-LAN reasoning as PostgREST without JWT and web-adapter without
a login) — that doc-ingestion-worker and notifier-scheduler call instead of
talking to MQTT/PostgREST themselves. See CLAUDE.md and the root README for
why: this used to be 5 services each holding their own MQTT/PostgREST/Claude
credentials; now only orchestrator (plus the two channel adapters, which keep
their own MQTT connection by design) does.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from shared.message import DocIngestionResult, NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from .claude_client import classify_intent
from .config import SERVICE_NAME, appconfig, mqtt_secrets, postgrest_secrets, system
from .conversation import get_or_create_conversation
from .flows.course import handle_quiz_answer, start_course
from .flows.onboarding import confirm_onboarding, handle_extraction_result, start_onboarding
from .flows.replacement import handle_replacement
from .flows.troubleshooting import handle_question
from .messaging import reply
from .reminders import check_reminders

logger = logging.getLogger("orchestrator")

_mqtt = ManagedMqttConnection("orchestrator")


async def handle_inbound(pg: PostgrestClient, mqtt: ManagedMqttConnection, payload: bytes) -> None:
    msg = NormalizedMessage.model_validate_json(payload)
    conversation = await get_or_create_conversation(pg, msg.channel, msg.conversation_id)
    pending_action = conversation["state"].get("pending_action")

    if msg.type.value == "photo" and msg.attachments:
        await start_onboarding(pg, mqtt, conversation, msg)
        return
    if msg.type.value == "photo":
        await reply(mqtt, msg, "No he recibido ninguna foto adjunta — vuelve a intentarlo.")
        return

    # Pending conversation state wins as long as a flow is open and waiting
    # for a specific reply from the user.
    if pending_action == "awaiting_confirmation":
        await confirm_onboarding(pg, mqtt, conversation, msg)
        return
    if pending_action == "awaiting_extraction":
        await reply(mqtt, msg, "Sigo analizando la foto anterior, dame un momento...")
        return
    if pending_action == "course_quiz":
        await handle_quiz_answer(pg, mqtt, conversation, msg)
        return

    # No open flow: delegate to Claude what the user wants (never our own
    # keyword-matching — CLAUDE.md section 10).
    classification = await classify_intent(msg.content or "")

    if classification.intent == "course":
        await start_course(pg, mqtt, conversation, msg, classification.topic or (msg.content or ""))
    elif classification.intent == "replacement":
        await handle_replacement(pg, mqtt, conversation, msg)
    else:
        await handle_question(pg, mqtt, conversation, msg)


# --- Internal HTTP API (doc-ingestion-worker, notifier-scheduler) ----------


async def handle_doc_ingestion_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = DocIngestionResult.model_validate(body)
    await handle_extraction_result(pg, _mqtt, result)
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
        "orchestrator starting up (PostgREST: %s, Claude model: %s)",
        postgrest_secrets.url,
        appconfig.get("claudeModel", "claude-sonnet-5"),
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


if __name__ == "__main__":
    asyncio.run(_run())
