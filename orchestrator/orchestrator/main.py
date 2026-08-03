"""orchestrator: stateless MQTT consumer — decides the intent and runs the right flow.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.
"""

from __future__ import annotations

import asyncio
import logging

from shared.message import DOC_INGESTION_RESULT_TOPIC, DocIngestionResult, NormalizedMessage
from shared.mqtt_client import maintain_mqtt_connection
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

logger = logging.getLogger("orchestrator")


async def handle_inbound(pg: PostgrestClient, mqtt, payload: bytes) -> None:
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
    classification = classify_intent(msg.content or "")

    if classification.intent == "course":
        await start_course(pg, mqtt, conversation, msg, classification.topic or (msg.content or ""))
    elif classification.intent == "replacement":
        await handle_replacement(pg, mqtt, conversation, msg)
    else:
        await handle_question(pg, mqtt, conversation, msg)


async def handle_doc_result(pg: PostgrestClient, mqtt, payload: bytes) -> None:
    result = DocIngestionResult.model_validate_json(payload)
    await handle_extraction_result(pg, mqtt, result)


async def main() -> None:
    logger.info("orchestrator starting up (PostgREST: %s, Claude model: %s)", postgrest_secrets.url, appconfig.get("claudeModel", "claude-sonnet-5"))
    pg = PostgrestClient(postgrest_secrets.url)

    async def on_connect(client) -> None:
        await client.subscribe("home/inbound/+/+")
        await client.subscribe(DOC_INGESTION_RESULT_TOPIC)
        async for message in client.messages:
            try:
                if str(message.topic) == DOC_INGESTION_RESULT_TOPIC:
                    await handle_doc_result(pg, client, message.payload)
                else:
                    await handle_inbound(pg, client, message.payload)
            except Exception:
                logger.exception("Error processing message from %s", message.topic)

    # appconfig hot-reloads in the background — the service's own parameters
    # (claudeModel, maxTokens, webSearchEnabled) are already read fresh on
    # every Claude call, so no extra hook is needed here.
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))
    try:
        await maintain_mqtt_connection(mqtt_secrets, system, on_connect)
    finally:
        config_task.cancel()
        await pg.aclose()


if __name__ == "__main__":
    asyncio.run(main())
