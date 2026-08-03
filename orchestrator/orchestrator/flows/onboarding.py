"""Flow 1: device onboarding.

photo -> doc-ingestion-worker (Claude vision extraction) -> confirmation with
the user -> saved to the registry (PostgREST).

Note: the reply strings below are intentionally left in Spanish — they're
what the assistant actually says to the household, and replies are meant to
mirror the language the user wrote in (today, that's Spanish). Everything
else in this file (comments, docstrings) is English per repo convention.
"""

from __future__ import annotations

from typing import Any

import aiomqtt

from shared.message import DOC_INGESTION_REQUEST_TOPIC, DocIngestionRequest, DocIngestionResult, NormalizedMessage
from shared.postgrest_client import PostgrestClient

from ..claude_client import interpret_confirmation
from ..conversation import clear_keys, get_conversation_by_id, update_state
from ..messaging import reply, reply_raw
from ..registry import create_device


def _format_draft(draft: dict[str, Any]) -> str:
    lines = [f"- Tipo: {draft.get('device_type_name', draft.get('device_type_code'))}"]
    if draft.get("brand"):
        lines.append(f"- Marca: {draft['brand']}")
    if draft.get("model"):
        lines.append(f"- Modelo: {draft['model']}")
    for key, value in (draft.get("attributes") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


async def start_onboarding(
    pg: PostgrestClient,
    mqtt: aiomqtt.Client,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
) -> None:
    request = DocIngestionRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=msg.conversation_id,
        channel=msg.channel,
        user_id=msg.user_id,
        attachment_url=msg.attachments[0],
    )
    await mqtt.publish(DOC_INGESTION_REQUEST_TOPIC, payload=request.model_dump_json(), qos=1)

    new_state = {**conversation["state"], "pending_action": "awaiting_extraction"}
    await update_state(pg, conversation["id"], new_state)
    await reply(mqtt, msg, "Recibido. Dame un momento para analizar la foto...")


async def handle_extraction_result(pg: PostgrestClient, mqtt: aiomqtt.Client, result: DocIngestionResult) -> None:
    conversation = await get_conversation_by_id(pg, result.conversation_id)

    if not result.success or not result.draft_device:
        await reply_raw(
            mqtt,
            result.channel,
            result.user_id,
            result.channel_conversation_id,
            f"No he podido leer la etiqueta ({result.error}). ¿Puedes probar con otra foto más clara?",
        )
        return

    new_state = {**conversation["state"], "pending_action": "awaiting_confirmation", "draft_device": result.draft_device}
    await update_state(pg, conversation["id"], new_state)

    summary = _format_draft(result.draft_device)
    await reply_raw(
        mqtt,
        result.channel,
        result.user_id,
        result.channel_conversation_id,
        f"He detectado esto:\n{summary}\n\n¿Es correcto? (sí/no)",
    )


async def confirm_onboarding(
    pg: PostgrestClient,
    mqtt: aiomqtt.Client,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
) -> None:
    draft = conversation["state"].get("draft_device")
    confirmed = interpret_confirmation(msg.content or "")

    if confirmed is None:
        # Ambiguous reply: keep the draft around so confirmation can be retried.
        await reply(mqtt, msg, "No lo he entendido — ¿confirmas los datos para guardarlos, o los descarto?")
        return

    cleared_state = clear_keys(conversation["state"], "pending_action", "draft_device")
    await update_state(pg, conversation["id"], cleared_state)

    if confirmed and draft:
        device = await create_device(pg, draft)
        await reply(mqtt, msg, f"Guardado: {device['display_name']}. Ya puedes preguntarme dudas sobre él.")
    else:
        await reply(mqtt, msg, "Vale, descartado. Puedes volver a enviar la foto cuando quieras.")
