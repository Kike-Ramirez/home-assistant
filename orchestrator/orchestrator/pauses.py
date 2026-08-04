"""Everything about pausing the agent loop on a tool call and resuming it
later — the `_kick_off_*` dispatch for each of the three genuinely-async
tools (`extract_device_data`, `generate_document`, `generate_image`) and for
the Human-in-the-Loop write confirmation, plus `finish_paused_turn`, the
shared tail end every resume path (a worker callback, or an approval
decision) funnels through.

Split out of `main.py` (which was growing indefinitely as more pause/resume
paths were added) — `runtime.py` holds what both this and `main.py` need so
neither imports the other.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any

import httpx
from shared.gemini_client import AgentTurnResult
from shared.message import (
    Attachment,
    AttachmentKind,
    DocGenerationRequest,
    DocIngestionRequest,
    ImageRequest,
    NormalizedMessage,
)
from shared.mqtt_client import ManagedMqttConnection
from shared.postgrest_client import PostgrestClient

from . import actions, llm, registry, security_guard
from .conversation import clear_keys, update_state
from .messaging import reply, reply_raw
from .runtime import PAUSE_TOOL_NAMES, doc_generation_client, doc_ingestion_client, image_generation_client, resume_agent_loop

logger = logging.getLogger("orchestrator")


async def kick_off_pending_tool(
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
        await update_state(pg, conversation, lambda s: {**s, "history": result.messages})
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
    def _pause(s: dict[str, Any]) -> dict[str, Any]:
        return {
            **s,
            "history": result.messages,
            "pending_agent_turn": tool_use["id"],
            "pending_attachments": [a.model_dump() for a in attachments],
        }

    conversation = await update_state(pg, conversation, _pause)

    request = DocIngestionRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        attachment_url=attachments[index].url_or_data,
    )
    # Fire-and-forget: doc-ingestion-worker accepts the job and replies later
    # via POST /internal/doc-ingestion/result (see handle_doc_ingestion_result).
    response = await doc_ingestion_client.post("/extract", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for
        # one. clear_keys, not a stale pre-pause snapshot: `conversation` here
        # is the row `_pause` just wrote, so explicitly undoing those two keys
        # is what actually reverts it, regardless of what else may have
        # changed concurrently in between.
        await update_state(pg, conversation, lambda s: clear_keys({**s, "history": result.messages}, "pending_agent_turn", "pending_attachments"))
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
    def _pause(s: dict[str, Any]) -> dict[str, Any]:
        return {
            **s,
            "history": result.messages,
            "pending_agent_turn": tool_use["id"],
            "pending_attachments": [a.model_dump() for a in attachments],
            # Carried through to the callback (handle_doc_generation_result) so
            # the rendered file can be auto-saved against the right device — or
            # as a general household document if the model didn't give one.
            "pending_device_id": tool_use["input"].get("device_id"),
        }

    # Persisted BEFORE firing the request, not after: doc-generation-worker's
    # rendering is pure local CPU (no external API call), fast enough that its
    # callback can race ahead of this write and find no pending_agent_turn yet
    # — silently dropped by handle_doc_generation_result ("no pending turn —
    # dropping"), leaving the conversation stuck forever waiting for a
    # callback that already happened and was ignored.
    conversation = await update_state(pg, conversation, _pause)

    request = DocGenerationRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        file_type=tool_use["input"]["file_type"],
        filename=tool_use["input"]["filename"],
        content=tool_use["input"]["content"],
    )
    response = await doc_generation_client.post("/generate", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for
        # one (see _kick_off_extraction's identical comment for why clear_keys,
        # not a stale pre-pause snapshot).
        await update_state(
            pg, conversation,
            lambda s: clear_keys({**s, "history": result.messages}, "pending_agent_turn", "pending_attachments", "pending_device_id"),
        )
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
        try:
            existing = await registry.get_latest_device_photo(pg, device_id)
        except Exception:
            # A DB hiccup here must never crash the whole turn (this call
            # happens outside the normal tool-dispatch try/except that every
            # other tool gets) — fall through to search/generation exactly as
            # if no existing photo had been found, same as a genuinely
            # missing/unreachable one below.
            logger.exception("Couldn't check for an existing photo on device %s — falling back to search/generation.", device_id)
            existing = None
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
            agent_result = await resume_agent_loop(pg, result.messages, {tool_use["id"]: tool_result})
            await finish_paused_turn(
                pg, mqtt, conversation, channel, user_id, channel_conversation_id, agent_result, attachment=attachment
            )
            return

    def _pause(s: dict[str, Any]) -> dict[str, Any]:
        return {
            **s,
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

    # Persisted BEFORE firing the request, not after — same race condition
    # `_kick_off_generation` guards against: a fast enough response (a real
    # search hit needs no generation at all) can otherwise race ahead of this
    # write and find no pending_agent_turn yet, silently dropped by
    # handle_image_generation_result ("no pending turn — dropping"), leaving
    # the conversation stuck forever waiting for a callback that already
    # happened and was ignored.
    conversation = await update_state(pg, conversation, _pause)

    request = ImageRequest(
        conversation_id=str(conversation["id"]),
        channel_conversation_id=channel_conversation_id,
        channel=channel,
        user_id=user_id,
        query=tool_use["input"]["query"],
        filename=tool_use["input"]["filename"],
    )
    response = await image_generation_client.post("/generate-image", json=request.model_dump())
    if response is None:
        # Dispatch never happened — no callback is ever coming, so revert the
        # pending state instead of leaving the conversation stuck waiting for
        # one (see _kick_off_extraction's identical comment for why clear_keys,
        # not a stale pre-pause snapshot).
        await update_state(
            pg, conversation,
            lambda s: clear_keys({**s, "history": result.messages}, "pending_agent_turn", "pending_attachments", "pending_device_id"),
        )
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
    await update_state(
        pg,
        conversation,
        lambda s: {
            **s,
            "history": result.messages,
            "pending_agent_turn": tool_use["id"],
            "pending_confirmation": {"tool_name": tool_use["name"], "tool_input": tool_use["input"]},
            "pending_attachments": [a.model_dump() for a in attachments],
        },
    )
    prompt = security_guard.confirmation_prompt(tool_use["name"], result.final_text)
    await reply_raw(mqtt, channel, user_id, channel_conversation_id, prompt, actions=security_guard.APPROVE_ACTIONS)


async def resolve_pending_confirmation(
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
    agent_result = await resume_agent_loop(pg, state.get("history", []), {pending_tool_use_id: tool_result})
    await finish_paused_turn(pg, mqtt, conversation, msg.channel, msg.user_id, msg.conversation_id, agent_result)


async def finish_paused_turn(
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
        tool_use = llm.client.find_tool_use(agent_result.messages, names=PAUSE_TOOL_NAMES)
        if tool_use is not None and tool_use["name"] in PAUSE_TOOL_NAMES:
            # The model asked for another paused tool in the same resumed turn
            # (e.g. the next photo in a multi-attachment message) — keep the
            # chain going instead of giving up (CLAUDE.md's former known gap #7).
            attachments = [Attachment.model_validate(a) for a in conversation["state"].get("pending_attachments", [])]
            continued_conversation = {**conversation, "state": {**conversation["state"], "history": agent_result.messages}}
            await kick_off_pending_tool(
                pg, mqtt, continued_conversation, channel, user_id, channel_conversation_id, attachments, agent_result, tool_use
            )
            return

        logger.warning("Agent loop paused again on resume with no recognized tool — conversation %s (degrading)", conversation["id"])
        await update_state(
            pg,
            conversation,
            lambda s: clear_keys(
                {**s, "history": agent_result.messages},
                "pending_agent_turn", "pending_confirmation", "pending_attachments", "pending_device_id",
            ),
        )
        if attachment is None:
            await reply_raw(mqtt, channel, user_id, channel_conversation_id, "He completado ese paso, pero necesito que me pidas el siguiente por separado.")
        return

    await update_state(
        pg,
        conversation,
        lambda s: clear_keys(
            {**s, "history": agent_result.messages},
            "pending_agent_turn", "pending_confirmation", "pending_attachments", "pending_device_id",
        ),
    )
    if attachment is None:
        await reply_raw(mqtt, channel, user_id, channel_conversation_id, agent_result.final_text or llm.DEFAULT_DONE_FALLBACK)


async def _save_onboarding_attachments(pg: PostgrestClient, device_id: str, pending_attachments: list[dict[str, Any]]) -> None:
    """Persists the photo(s)/document(s) that led to `device_id` being
    created or edited — `pending_attachments` (still in `conversation.state`
    at the moment a `create_device`/`update_device` confirmation resolves)
    are otherwise never kept anywhere once `extract_device_data` has pulled
    the structured data out of them (CLAUDE.md section 10). Best-effort, same
    reasoning as `save_generated_attachment` — never blocks the actual
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


async def save_generated_attachment(pg: PostgrestClient, device_id: str | None, kind: str, attachment: Attachment) -> None:
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
