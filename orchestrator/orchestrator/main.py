"""orchestrator: the single point of contact for MQTT, PostgREST, and Gemini.

Conversation state always lives in Postgres (via PostgREST), never in process
memory (CLAUDE.md section 4) — that's what lets this scale to replicas.

Every inbound message, whatever it contains (text, an image, a document, a
pasted URL), goes straight into `llm.client.run_agent_loop` (`shared.gemini_client`)
with the full tool surface from `actions.py`. Orchestrator itself never decides
what a message means — it only builds the user message, runs the loop,
executes whatever the model calls, and persists the result (CLAUDE.md section
10, "the model drives via tool-use"). Three things can make the loop pause
without executing a tool (`actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES`):

- `extract_device_data`: requires an HTTP round trip to doc-ingestion-worker
  that can't be awaited inline — kicked off here, resumed when that service
  calls back (`handle_doc_ingestion_result`).
- `generate_document`: same shape, an HTTP round trip to doc-generation-worker
  to render a file the model already wrote the content for — resumed when
  that service calls back (`handle_doc_generation_result`), and the rendered
  file is delivered to the user as an attachment on that same reply.
- `generate_image`: if `device_id` is given, first checks for a photo
  already attached to that device (`registry.get_latest_device_photo`) and,
  if one's found and still fetchable, resolves immediately with no worker
  round trip at all. Otherwise same shape as the other two: an HTTP round
  trip to image-generation-worker (real image search first, Gemini
  generation as a fallback) — resumed when that service calls back
  (`handle_image_generation_result`). Either way, delivered as a Telegram
  photo (not a generic file) via `AttachmentKind.IMAGE`.
- `create_device` / `update_device` / `retire_device`: writes to the
  inventory, gated on the user's explicit approval (Human-in-the-Loop) —
  sent out as a channel message with `actions` (Aprobar/Rechazar buttons) —
  or, on channels without real buttons, a plain sí/no text reply works too
  (`security_guard.parse_confirmation_text`) — resumed when the decision
  comes back in (`_resolve_pending_confirmation`).

A single inbound message can carry several attachments (e.g. a Telegram
album of photos) — `conversation.state.pending_attachments` keeps the
original message's attachments available across however many pause/resume
round trips it takes for the model to work through all of them (one at a
time: `_finish_paused_turn` re-enters `_kick_off_pending_tool` on every
resume, instead of giving up after the first one — see CLAUDE.md's former
known-gap #7).

Runs two independent things side by side: the MQTT connection (consuming
`home/inbound/+/+`, same as before) and a small internal HTTP API — reachable
only on the `barbaraServices` Docker network, no host port published, no auth
(same trusted-LAN reasoning as PostgREST without JWT and web-adapter without
a login) — that doc-ingestion-worker, doc-generation-worker, and
image-generation-worker call instead of talking to MQTT/PostgREST themselves.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

import httpx
from aiohttp import web
from shared.gemini_client import AgentTurnResult
from shared.internal_client import InternalApiClient
from shared.message import (
    Attachment,
    AttachmentKind,
    DocGenerationRequest,
    DocGenerationResult,
    DocIngestionRequest,
    DocIngestionResult,
    ImageRequest,
    ImageResult,
    MessageType,
    NormalizedMessage,
)
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from . import actions, llm, registry, security_guard
from .config import (
    SERVICE_NAME,
    appconfig,
    doc_generation_worker_secrets,
    doc_ingestion_worker_secrets,
    image_generation_worker_secrets,
    mqtt_secrets,
    postgrest_secrets,
    system,
    welcome_secrets,
)
from .conversation import clear_keys, get_conversation_by_id, get_or_create_conversation, update_state
from .messaging import reply, reply_raw

logger = logging.getLogger("orchestrator")

_mqtt = ManagedMqttConnection("orchestrator")
_doc_ingestion_client = InternalApiClient(doc_ingestion_worker_secrets.url, "orchestrator")
_doc_generation_client = InternalApiClient(doc_generation_worker_secrets.url, "orchestrator")
_image_generation_client = InternalApiClient(image_generation_worker_secrets.url, "orchestrator")

_PAUSE_TOOL_NAMES = actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES

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


def _loop_kwargs() -> dict[str, Any]:
    """Shared config for every `run_agent_loop`/`resume_agent_loop` call — one
    spot to read live from appconfig instead of the two call sites drifting."""
    return {
        "max_tokens": llm.max_tokens(),
        "async_tool_names": _PAUSE_TOOL_NAMES,
        "max_iterations_fallback": llm.MAX_ITERATIONS_FALLBACK,
        "web_search": appconfig.get("webSearchEnabled", True),
        "api_error_fallback": llm.API_ERROR_FALLBACK,
    }


async def _run_agent_loop(pg: PostgrestClient, history: list[dict[str, Any]]) -> AgentTurnResult:
    return await llm.client.run_agent_loop(
        llm.SYSTEM_PROMPT, actions.TOOL_SCHEMAS, actions.make_executor(pg), history, **_loop_kwargs()
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
        **_loop_kwargs(),
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
            await reply(mqtt, msg, "Sigo con lo anterior, dame un momento...")
        return

    history: list[dict[str, Any]] = state.get("history", [])
    user_message = await llm.client.build_user_message(msg.content, msg.attachments)
    history = [*history, user_message]

    result = await _run_agent_loop(pg, history)

    if result.done:
        await update_state(pg, conversation["id"], {**state, "history": result.messages})
        await reply(mqtt, msg, result.final_text or llm.DEFAULT_DONE_FALLBACK)
        return

    tool_use = llm.client.find_tool_use(result.messages, names=_PAUSE_TOOL_NAMES)
    if tool_use is None:
        logger.error(
            "Agent loop paused with no pending tool_use — conversation %s. Likely cause: run_agent_loop "
            "returned done=False without a function_call in the last part — check GeminiClient._loop in "
            "shared/shared/gemini_client.py.",
            conversation["id"],
        )
        await update_state(pg, conversation["id"], {**state, "history": result.messages})
        await reply(mqtt, msg, "Algo ha ido mal — inténtalo de nuevo.")
        return

    await _kick_off_pending_tool(
        pg, mqtt, conversation, msg.channel, msg.user_id, msg.conversation_id, msg.attachments, result, tool_use
    )


async def _kick_off_pending_tool(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    attachments: list[Attachment],
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Dispatches a just-paused tool call to whichever kickoff it needs.
    The one entry point for starting a pause, whether it's the first one for
    an inbound message or another one right after a resume (a message with
    several attachments routinely chains a few of these back to back)."""
    name = tool_use["name"]
    if name == "extract_device_data":
        await _kick_off_extraction(pg, mqtt, conversation, channel, user_id, channel_conversation_id, attachments, result, tool_use)
    elif name == "generate_document":
        await _kick_off_generation(pg, mqtt, conversation, channel, user_id, channel_conversation_id, attachments, result, tool_use)
    elif name == "generate_image":
        await _kick_off_image_generation(pg, mqtt, conversation, channel, user_id, channel_conversation_id, attachments, result, tool_use)
    elif name in actions.CONFIRM_TOOL_NAMES:
        await _kick_off_confirmation(pg, mqtt, conversation, channel, user_id, channel_conversation_id, attachments, result, tool_use)
    else:
        logger.error(
            "Agent loop paused on unexpected tool %r — conversation %s. Likely cause: this tool isn't in "
            "ASYNC_TOOL_NAMES/CONFIRM_TOOL_NAMES (actions.py) but the model is treating it as pausable — "
            "add it to one of those two sets if that's intentional.",
            name,
            conversation["id"],
        )
        await update_state(pg, conversation["id"], {**conversation["state"], "history": result.messages})
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, "Algo ha ido mal — inténtalo de nuevo.")


async def _kick_off_extraction(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    attachments: list[Attachment],
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Fires the actual `/extract` request for the `extract_device_data` tool
    call the loop just paused on. Doesn't touch `conversation.state` at all
    on failure — an unresolvable pending turn would just hang forever waiting
    for a callback that's never coming, so the safest thing is to leave the
    conversation exactly as it was before this message."""
    index = tool_use["input"].get("attachment_index", 0)
    if not attachments or index >= len(attachments):
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, "No he recibido ninguna foto que analizar — vuelve a intentarlo.")
        return

    # `pending_agent_turn` carries the pending tool_use_id itself (not just a
    # boolean) — resume_agent_loop resolves `resolved_tool_results` by that id
    # once the callback below has the extraction result. `pending_attachments`
    # keeps the original message's attachments around across however many more
    # pause/resume round trips this message ends up needing (one per attachment).
    #
    # Persisted BEFORE firing the request, not after: doc-ingestion-worker's
    # callback can otherwise race ahead of this write (plausible once its own
    # work is fast) and find no pending_agent_turn yet, silently dropping the
    # result (handle_doc_ingestion_result's "no pending turn — dropping") and
    # leaving the conversation stuck forever waiting for a callback that
    # already happened and was ignored.
    paused_state = {
        **conversation["state"],
        "history": result.messages,
        "pending_agent_turn": tool_use["id"],
        "pending_attachments": [a.model_dump() for a in attachments],
    }
    await update_state(pg, conversation["id"], paused_state)

    request = DocIngestionRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        attachment_url=attachments[index].url_or_data,
    )
    # Fire-and-forget: doc-ingestion-worker accepts the job and replies later
    # via POST /internal/doc-ingestion/result (see handle_doc_ingestion_result).
    response = await _doc_ingestion_client.post("/extract", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for one.
        await update_state(pg, conversation["id"], {**conversation["state"], "history": result.messages})
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, "No puedo procesar la foto ahora mismo — inténtalo de nuevo en un momento.")
        return

    await reply_raw(mqtt, channel, user_id, channel_conversation_id, result.final_text or "Recibido. Dame un momento para analizar la foto...")


async def _kick_off_generation(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    attachments: list[Attachment],
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Fires the actual `/generate` request for the `generate_document` tool
    call the loop just paused on — the model has already written the file's
    content, this only asks doc-generation-worker to render it into bytes."""
    # Persisted BEFORE firing the request, not after: doc-generation-worker's
    # rendering is pure local CPU (no external API call), fast enough that its
    # callback can race ahead of this write and find no pending_agent_turn yet
    # — silently dropped by handle_doc_generation_result ("no pending turn —
    # dropping"), leaving the conversation stuck forever waiting for a
    # callback that already happened and was ignored.
    paused_state = {
        **conversation["state"],
        "history": result.messages,
        "pending_agent_turn": tool_use["id"],
        "pending_attachments": [a.model_dump() for a in attachments],
        # Carried through to the callback (handle_doc_generation_result) so
        # the rendered file can be auto-saved against the right device — or
        # as a general household document if the model didn't give one.
        "pending_device_id": tool_use["input"].get("device_id"),
    }
    await update_state(pg, conversation["id"], paused_state)

    request = DocGenerationRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        file_type=tool_use["input"]["file_type"],
        filename=tool_use["input"]["filename"],
        content=tool_use["input"]["content"],
    )
    response = await _doc_generation_client.post("/generate", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for one.
        await update_state(pg, conversation["id"], {**conversation["state"], "history": result.messages})
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, "No puedo generar el documento ahora mismo — inténtalo de nuevo en un momento.")
        return

    await reply_raw(mqtt, channel, user_id, channel_conversation_id, result.final_text or "Generando tu documento, dame un momento...")


def _image_filename(name: str) -> str:
    """Same sanitization as image-generation-worker's own `_safe_filename` —
    kept in sync deliberately rather than shared, since this one only ever
    handles the "reuse an existing photo" path, not a whole service."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return (slug or "imagen") + ".jpg"


async def _fetch_existing_device_photo(url_or_ref: str) -> tuple[bytes, str] | None:
    """Resolves a `device_document.url_or_ref` into real bytes — decodes it
    directly if it's already a `data:` URI, downloads it if it's a URL. The
    download also doubles as a reachability check: a Telegram file URL
    attached a while ago may well have expired by now (Telegram's own file
    links are short-lived), so this returns `None` on any failure instead of
    letting a stale reference break the reply — the caller falls back to
    search/generation exactly as if no photo had ever been on file.

    Not re-encoded to JPEG here (unlike image-generation-worker's own
    `convert.py`) — a deliberate simplification: every photo Telegram itself
    forwards is always already a real JPEG (Telegram re-encodes any photo it
    receives), which covers the overwhelming majority of attached device
    photos in practice; only a non-Telegram (web-adapter) upload could be a
    different format, an edge case not worth a second Pillow dependency for.
    """
    try:
        if url_or_ref.startswith("data:"):
            header, _, b64_data = url_or_ref.partition(",")
            media_type = header.removeprefix("data:").split(";")[0] or "image/jpeg"
            return base64.b64decode(b64_data), media_type
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url_or_ref, follow_redirects=True)
            response.raise_for_status()
            media_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
            return response.content, media_type
    except Exception:
        logger.warning("Existing device photo at %r isn't fetchable anymore — falling back to search/generation.", url_or_ref[:200])
        return None


async def _kick_off_image_generation(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    attachments: list[Attachment],
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Handles the `generate_image` tool call the loop just paused on.

    If `device_id` was given and a photo is already attached to that device
    (and still fetchable), reuses it immediately — resolves the tool call
    right here with no worker round trip, same as an instant, already-decided
    confirmation. Otherwise fires the actual `/generate-image` request:
    image-generation-worker tries a real image search first, falls back to
    Gemini generation, and always normalizes that result to JPEG before
    calling back."""
    device_id = tool_use["input"].get("device_id")
    if device_id:
        existing = await registry.get_latest_device_photo(pg, device_id)
        fetched = await _fetch_existing_device_photo(existing["url_or_ref"]) if existing else None
        if fetched is not None:
            data, media_type = fetched
            filename = _image_filename(tool_use["input"]["filename"])
            attachment = Attachment(
                kind=AttachmentKind.IMAGE,
                media_type=media_type,
                url_or_data=f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}",
                filename=filename,
            )
            tool_result = {"success": True, "filename": filename, "source": "existing"}
            agent_result = await _resume_agent_loop(pg, result.messages, {tool_use["id"]: tool_result})
            await _finish_paused_turn(
                pg, mqtt, conversation, channel, user_id, channel_conversation_id, agent_result, attachment=attachment
            )
            return

    # Persisted BEFORE firing the request, not after — same race condition
    # `_kick_off_generation` guards against: a fast enough response (a real
    # search hit needs no generation at all) can otherwise race ahead of this
    # write and find no pending_agent_turn yet, silently dropped by
    # handle_image_generation_result ("no pending turn — dropping"), leaving
    # the conversation stuck forever waiting for a callback that already
    # happened and was ignored.
    paused_state = {
        **conversation["state"],
        "history": result.messages,
        "pending_agent_turn": tool_use["id"],
        "pending_attachments": [a.model_dump() for a in attachments],
        # Carried through to the callback (handle_image_generation_result) so
        # the result can be auto-saved against the right device — or as a
        # general household image if the model didn't give one. Not needed
        # on the instant-reuse path above (an already-saved photo, nothing
        # new to save).
        "pending_device_id": tool_use["input"].get("device_id"),
    }
    await update_state(pg, conversation["id"], paused_state)

    request = ImageRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        query=tool_use["input"]["query"],
        filename=tool_use["input"]["filename"],
    )
    response = await _image_generation_client.post("/generate-image", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for one.
        await update_state(pg, conversation["id"], {**conversation["state"], "history": result.messages})
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, "No puedo conseguir la imagen ahora mismo — inténtalo de nuevo en un momento.")
        return

    await reply_raw(mqtt, channel, user_id, channel_conversation_id, result.final_text or "Buscando la imagen, dame un momento...")


async def _kick_off_confirmation(
    pg: PostgrestClient,
    mqtt: ManagedMqttConnection,
    conversation: dict[str, Any],
    channel: str,
    user_id: str,
    channel_conversation_id: str,
    attachments: list[Attachment],
    result: AgentTurnResult,
    tool_use: dict[str, Any],
) -> None:
    """Pauses the loop on a write tool (`actions.CONFIRM_TOOL_NAMES`) and asks
    the user to approve/reject it via the channel (Human-in-the-Loop, see
    `security_guard.py`) before it's actually dispatched. Only one action can
    be pending per conversation at a time — same constraint
    `extract_device_data`/`generate_document` already have — so the button
    press just needs to say "approve"/"reject", no correlation id."""
    new_state = {
        **conversation["state"],
        "history": result.messages,
        "pending_agent_turn": tool_use["id"],
        "pending_confirmation": {"tool_name": tool_use["name"], "tool_input": tool_use["input"]},
        "pending_attachments": [a.model_dump() for a in attachments],
    }
    await update_state(pg, conversation["id"], new_state)
    prompt = security_guard.confirmation_prompt(tool_use["name"], result.final_text)
    await reply_raw(mqtt, channel, user_id, channel_conversation_id, prompt, actions=security_guard.APPROVE_ACTIONS)


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
    if pending["tool_name"] in ("create_device", "update_device") and isinstance(tool_result, dict) and tool_result.get("id"):
        # Whatever photo/document led to this device being created or edited
        # is still in pending_attachments at this point (carried through
        # every pause/resume in this message's chain) — save it now that
        # it's actually associated with a device, instead of only ever
        # existing as extracted data (CLAUDE.md section 10).
        await _save_onboarding_attachments(pg, tool_result["id"], state.get("pending_attachments", []))
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
    attachment: Attachment | None = None,
) -> None:
    """Shared tail end for every resume path (doc-ingestion callback,
    doc-generation callback, image-generation callback, and approval
    callback): deliver a just-rendered file/image if there is one, then
    either continue the chain (another attachment or action the model wants
    to handle next) or close out the turn."""
    if attachment is not None:
        # The file/image is ready now — send it as soon as it's ready,
        # regardless of whatever the model wants to do next in this same
        # resumed turn.
        default_text = "Aquí tienes tu imagen." if attachment.kind == AttachmentKind.IMAGE else "Aquí tienes tu documento."
        await reply_raw(
            mqtt, channel, user_id, channel_conversation_id, agent_result.final_text or default_text, attachments=[attachment]
        )

    if not agent_result.done:
        tool_use = llm.client.find_tool_use(agent_result.messages, names=_PAUSE_TOOL_NAMES)
        if tool_use is not None and tool_use["name"] in _PAUSE_TOOL_NAMES:
            # The model asked for another paused tool in the same resumed turn
            # (e.g. the next photo in a multi-attachment message) — keep the
            # chain going instead of giving up (CLAUDE.md's former known gap #7).
            attachments = [Attachment.model_validate(a) for a in conversation["state"].get("pending_attachments", [])]
            continued_conversation = {**conversation, "state": {**conversation["state"], "history": agent_result.messages}}
            await _kick_off_pending_tool(
                pg, mqtt, continued_conversation, channel, user_id, channel_conversation_id, attachments, agent_result, tool_use
            )
            return

        logger.warning("Agent loop paused again on resume with no recognized tool — conversation %s (degrading)", conversation["id"])
        new_state = clear_keys(
            {**conversation["state"], "history": agent_result.messages},
            "pending_agent_turn",
            "pending_confirmation",
            "pending_attachments",
            "pending_device_id",
        )
        await update_state(pg, conversation["id"], new_state)
        if attachment is None:
            await reply_raw(mqtt, channel, user_id, channel_conversation_id, "He completado ese paso, pero necesito que me pidas el siguiente por separado.")
        return

    new_state = clear_keys(
        {**conversation["state"], "history": agent_result.messages},
        "pending_agent_turn",
        "pending_confirmation",
        "pending_attachments",
        "pending_device_id",
    )
    await update_state(pg, conversation["id"], new_state)
    if attachment is None:
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, agent_result.final_text or llm.DEFAULT_DONE_FALLBACK)


async def _save_onboarding_attachments(pg: PostgrestClient, device_id: str, pending_attachments: list[dict[str, Any]]) -> None:
    """Persists the photo(s)/document(s) that led to `device_id` being
    created or edited — `pending_attachments` (still in `conversation.state`
    at the moment a `create_device`/`update_device` confirmation resolves)
    are otherwise never kept anywhere once `extract_device_data` has pulled
    the structured data out of them (CLAUDE.md section 10). Best-effort, same
    reasoning as `_save_generated_attachment` — never blocks the actual
    device write, which has already happened by the time this runs."""
    for raw in pending_attachments:
        attachment = Attachment.model_validate(raw)
        if attachment.kind == AttachmentKind.AUDIO:
            continue
        kind = "photo" if attachment.kind == AttachmentKind.IMAGE else "manual"
        try:
            await registry.add_device_document(
                pg,
                device_id,
                kind,
                attachment.url_or_data,
                description="Foto/documento usado para dar de alta o editar este dispositivo",
                media_type=attachment.media_type,
            )
        except Exception:
            logger.exception("Couldn't save an onboarding attachment for device %s — the device write is unaffected", device_id)


async def _save_generated_attachment(pg: PostgrestClient, device_id: str | None, kind: str, attachment: Attachment) -> None:
    """Persists anything Gemini sends back to the user (a generated report or
    image) as a `device_document`, so it's available later exactly like
    anything the user attached themselves — against `device_id` if the model
    gave one, or as a general household document (`device_id=None`) if it
    didn't (CLAUDE.md section 10). Best-effort: a failure here shouldn't ever
    stop the file from actually reaching the user, so it's logged and
    swallowed rather than propagated."""
    try:
        await registry.add_device_document(
            pg,
            device_id,
            kind,
            attachment.url_or_data,
            description=f"Generado por Gemini: {attachment.filename}",
            media_type=attachment.media_type,
        )
    except Exception:
        logger.exception("Couldn't save the generated %s %r for future reference — delivery is unaffected", kind, attachment.filename)


# --- Internal HTTP API (doc-ingestion-worker, doc-generation-worker, image-generation-worker) ---


async def handle_doc_ingestion_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = DocIngestionResult.model_validate(body)
    conversation = await get_conversation_by_id(pg, result.conversation_id)
    pending_tool_use_id = conversation["state"].get("pending_agent_turn")
    if not pending_tool_use_id:
        # Nothing to resume — the conversation moved on (or was never
        # pending) before this callback arrived. Retrying wouldn't help, so
        # accept the request and drop it instead of a raw KeyError/500.
        logger.warning(
            "doc-ingestion callback for conversation %s with no pending turn — dropping", result.conversation_id
        )
        return web.json_response({"ok": False})

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


async def handle_doc_generation_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = DocGenerationResult.model_validate(body)
    conversation = await get_conversation_by_id(pg, result.conversation_id)
    pending_tool_use_id = conversation["state"].get("pending_agent_turn")
    if not pending_tool_use_id:
        logger.warning(
            "doc-generation callback for conversation %s with no pending turn — dropping", result.conversation_id
        )
        return web.json_response({"ok": False})

    attachment: Attachment | None = None
    if result.success and result.data_base64:
        tool_result: Any = {"success": True, "filename": result.filename}
        attachment = Attachment(
            kind=AttachmentKind.DOCUMENT,
            media_type=result.media_type,
            url_or_data=f"data:{result.media_type};base64,{result.data_base64}",
            filename=result.filename,
        )
        await _save_generated_attachment(
            pg, conversation["state"].get("pending_device_id"), kind="report", attachment=attachment
        )
    else:
        tool_result = {"success": False, "error": result.error or "unknown error"}

    agent_result = await _resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await _finish_paused_turn(
        pg, _mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result, attachment=attachment
    )

    return web.json_response({"ok": True})


async def handle_image_generation_result(request: web.Request) -> web.Response:
    pg: PostgrestClient = request.app["pg"]
    body = await request.json()
    result = ImageResult.model_validate(body)
    conversation = await get_conversation_by_id(pg, result.conversation_id)
    pending_tool_use_id = conversation["state"].get("pending_agent_turn")
    if not pending_tool_use_id:
        logger.warning(
            "image-generation callback for conversation %s with no pending turn — dropping", result.conversation_id
        )
        return web.json_response({"ok": False})

    attachment: Attachment | None = None
    if result.success and result.data_base64:
        tool_result: Any = {"success": True, "filename": result.filename, "source": result.source}
        attachment = Attachment(
            kind=AttachmentKind.IMAGE,
            media_type="image/jpeg",
            url_or_data=f"data:image/jpeg;base64,{result.data_base64}",
            filename=result.filename,
        )
        await _save_generated_attachment(
            pg, conversation["state"].get("pending_device_id"), kind="photo", attachment=attachment
        )
    else:
        tool_result = {"success": False, "error": result.error or "unknown error"}

    agent_result = await _resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await _finish_paused_turn(
        pg, _mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result, attachment=attachment
    )

    return web.json_response({"ok": True})


def build_app(pg: PostgrestClient) -> web.Application:
    app = web.Application()
    app["pg"] = pg
    app.router.add_post("/internal/doc-ingestion/result", handle_doc_ingestion_result)
    app.router.add_post("/internal/doc-generation/result", handle_doc_generation_result)
    app.router.add_post("/internal/image/result", handle_image_generation_result)
    return app


async def _send_welcome_message() -> None:
    if not welcome_secrets.admin_chat_id:
        logger.warning(
            "TELEGRAM_ADMIN_CHAT_ID isn't set — skipping the startup welcome message. Suggested fix: add "
            "that variable to barbarasecrets.env with your own Telegram chat id."
        )
        return
    await reply_raw(_mqtt, "telegram", welcome_secrets.admin_chat_id, welcome_secrets.admin_chat_id, WELCOME_MESSAGE)


async def _run() -> None:
    pg = PostgrestClient(postgrest_secrets.url)
    _ready_logged = False
    _http_ready = asyncio.Event()

    async def on_mqtt_connect(client) -> None:
        nonlocal _ready_logged
        _mqtt.bind(client)
        if not _ready_logged:
            _ready_logged = True
            await _http_ready.wait()  # so "ready" is only logged once BOTH MQTT and the internal API are up
            logger.info("orchestrator ready: MQTT connected, PostgREST reachable, Gemini engine configured.")
            await _send_welcome_message()
        await _mqtt.consume(client, "home/inbound/+/+", lambda message: handle_inbound(pg, _mqtt, message.payload))

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
        await _doc_ingestion_client.aclose()
        await _doc_generation_client.aclose()
        await _image_generation_client.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
