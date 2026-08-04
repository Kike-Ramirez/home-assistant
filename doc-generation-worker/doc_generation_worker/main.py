"""doc-generation-worker: renders a document `orchestrator` already wrote the
content for, into actual file bytes, over HTTP.

Mirrors doc-ingestion-worker's shape exactly, mirrored direction: that service
turns a document into data (vision extraction), this one turns data (text the
model wrote) into a document. No MQTT, no Postgres, no LLM client of its own —
orchestrator fires a job at `POST /generate` (fire-and-forget — this responds
immediately, the actual rendering happens in the background), and this
service calls back to orchestrator's `POST /internal/doc-generation/result`
once it's done.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from aiohttp import web
from shared.internal_client import InternalApiClient
from shared.message import DocGenerationRequest, DocGenerationResult
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .renderer import EXTENSIONS, MEDIA_TYPES, render

logger = logging.getLogger("doc_generation_worker")

_max_concurrency = appconfig.get("maxConcurrency", 2)
_semaphore = asyncio.Semaphore(_max_concurrency)
_orchestrator_client = InternalApiClient(orchestrator_secrets.url, SERVICE_NAME)

_background_tasks: set[asyncio.Task] = set()


async def _process(request: DocGenerationRequest) -> None:
    try:
        async with _semaphore:
            try:
                data = render(request.file_type, request.content)
                filename = request.filename
                if not filename.endswith(EXTENSIONS[request.file_type]):
                    filename += EXTENSIONS[request.file_type]
                result = DocGenerationResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=True,
                    filename=filename,
                    media_type=MEDIA_TYPES[request.file_type],
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            except Exception as exc:  # noqa: BLE001 — any failure needs to be reported back to the user
                logger.exception(
                    "Error rendering a document — common cause: unexpected file_type/content in "
                    "request.content — see render() in render.py for the exact format it expects."
                )
                result = DocGenerationResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=False,
                    error=str(exc),
                )

        await _orchestrator_client.post("/internal/doc-generation/result", json=result.model_dump())
    except Exception:
        logger.exception("Unrecoverable error processing a doc-generation request")


async def handle_generate(request: web.Request) -> web.Response:
    body = await request.json()
    gen_request = DocGenerationRequest.model_validate(body)

    task = asyncio.create_task(_process(gen_request))
    # Strong reference until it's done — otherwise the event loop could
    # garbage-collect the task mid-flight (asyncio docs, "Important").
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return web.json_response({"accepted": True}, status=202)


async def _on_config_change(changed_keys: set[str]) -> None:
    if "maxConcurrency" in changed_keys:
        global _max_concurrency, _semaphore
        _max_concurrency = appconfig.get("maxConcurrency", 2)
        _semaphore = asyncio.Semaphore(_max_concurrency)
        logger.info("Max concurrency updated to %s (semaphore recreated)", _max_concurrency)


async def on_startup(app: web.Application) -> None:
    logger.info("doc-generation-worker ready (max concurrency: %s).", _max_concurrency)
    app["config_task"] = asyncio.create_task(
        watch_appconfig(SERVICE_NAME, system, appconfig, on_change=_on_config_change)
    )


async def on_shutdown(app: web.Application) -> None:
    app["config_task"].cancel()
    await _orchestrator_client.aclose()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_post("/generate", handle_generate)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
