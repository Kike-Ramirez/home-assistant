"""Flow 1: device onboarding.

photo -> doc-ingestion-worker (Claude vision extraction, over its `/extract`
HTTP endpoint) -> confirmation with the user -> saved to the registry
(PostgREST).

Note: the reply strings below are intentionally left in Spanish — they're
what the assistant actually says to the household, and replies are meant to
mirror the language the user wrote in (today, that's Spanish). Everything
else in this file (comments, docstrings) is English per repo convention.
"""

from __future__ import annotations

from typing import Any

from shared.internal_client import InternalApiClient
from shared.message import DocIngestionRequest, DocIngestionResult, NormalizedMessage
from shared.mqtt_client import ManagedMqttConnection
from shared.postgrest_client import PostgrestClient

from ..claude_client import interpret_confirmation
from ..config import doc_ingestion_worker_secrets
from ..conversation import clear_keys, get_conversation_by_id, update_state
from ..messaging import reply, reply_raw
from ..registry import create_device

# orchestrator is the one firing the extraction job — same "one client per
# external dependency, created once" pattern as claude_client.py.
_doc_ingestion_client = InternalApiClient(doc_ingestion_worker_secrets.url, "orchestrator")


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
    mqtt: ManagedMqttConnection,
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
    # Fire-and-forget: doc-ingestion-worker accepts the job and replies later
    # via POST /internal/doc-ingestion/result — same async decoupling MQTT used
    # to give us, just over HTTP now (see shared/internal_client.py).
    response = await _doc_ingestion_client.post("/extract", json=request.model_dump())

    if response is None:
        # InternalApiClient already logged the failure after its own retries —
        # degrade gracefully instead of leaving the user hanging.
        await reply(mqtt, msg, "No puedo procesar la foto ahora mismo — inténtalo de nuevo en un momento.")
        return

    new_state = {**conversation["state"], "pending_action": "awaiting_extraction"}
    await update_state(pg, conversation["id"], new_state)
    await reply(mqtt, msg, "Recibido. Dame un momento para analizar la foto...")


async def handle_extraction_result(pg: PostgrestClient, mqtt: ManagedMqttConnection, result: DocIngestionResult) -> None:
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
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    msg: NormalizedMessage,
) -> None:
    draft = conversation["state"].get("draft_device")
    confirmed = await interpret_confirmation(msg.content or "")

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
