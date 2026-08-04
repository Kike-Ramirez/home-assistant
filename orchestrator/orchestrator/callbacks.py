"""The internal HTTP API (doc-ingestion-worker, doc-generation-worker,
image-generation-worker) — each worker's fire-and-forget job calls back here
once it's done, resuming whatever agent-loop turn paused waiting for it
(`pauses.py::finish_paused_turn`).

Split out of `main.py` alongside `pauses.py` — see that module's docstring
for why, and `runtime.py` for the shared objects neither of them (nor
`main.py`) needs to import the other two for.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from shared.message import (
    Attachment,
    AttachmentKind,
    DocGenerationResult,
    DocIngestionResult,
    ImageResult,
)
from shared.postgrest_client import PostgrestClient

from .conversation import get_conversation_by_id
from .pauses import finish_paused_turn, save_generated_attachment
from .runtime import mqtt, resume_agent_loop

logger = logging.getLogger("orchestrator")


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

    agent_result = await resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await finish_paused_turn(
        pg, mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result
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
        await save_generated_attachment(
            pg, conversation["state"].get("pending_device_id"), kind="report", attachment=attachment
        )
    else:
        tool_result = {"success": False, "error": result.error or "unknown error"}

    agent_result = await resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await finish_paused_turn(
        pg, mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result, attachment=attachment
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
        await save_generated_attachment(
            pg, conversation["state"].get("pending_device_id"), kind="photo", attachment=attachment
        )
    else:
        tool_result = {"success": False, "error": result.error or "unknown error"}

    agent_result = await resume_agent_loop(
        pg, conversation["state"].get("history", []), {pending_tool_use_id: tool_result}
    )
    await finish_paused_turn(
        pg, mqtt, conversation, result.channel, result.user_id, result.channel_conversation_id, agent_result, attachment=attachment
    )

    return web.json_response({"ok": True})


def build_app(pg: PostgrestClient) -> web.Application:
    app = web.Application()
    app["pg"] = pg
    app.router.add_post("/internal/doc-ingestion/result", handle_doc_ingestion_result)
    app.router.add_post("/internal/doc-generation/result", handle_doc_generation_result)
    app.router.add_post("/internal/image/result", handle_image_generation_result)
    return app
