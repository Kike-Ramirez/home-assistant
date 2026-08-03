"""doc-ingestion-worker: extraction jobs over HTTP, bounded concurrency.

No MQTT connection anymore (see CLAUDE.md, "orchestrator owns every external
connection"): orchestrator fires a job at `POST /extract` (fire-and-forget —
this responds immediately, the actual extraction happens in the background),
and this service calls back to orchestrator's
`POST /internal/doc-ingestion/result` once it's done. Still doesn't block the
normal chat flow (extraction can take a while) — that's what the semaphore is
for, same as before.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from shared.internal_client import InternalApiClient
from shared.message import DocIngestionRequest, DocIngestionResult
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .extractor import extract_device_data

logger = logging.getLogger("doc_ingestion_worker")

_max_concurrency = appconfig.get("maxConcurrency", 2)
_semaphore = asyncio.Semaphore(_max_concurrency)
_orchestrator_client = InternalApiClient(orchestrator_secrets.url, SERVICE_NAME)

_background_tasks: set[asyncio.Task] = set()


async def _process(request: DocIngestionRequest) -> None:
    # The whole body is covered by the try — including the final callback, so
    # a network failure while replying doesn't get lost as an orphaned
    # exception in a fire-and-forget task.
    try:
        async with _semaphore:
            try:
                draft = await extract_device_data(request.attachment_url)
                result = DocIngestionResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=True,
                    draft_device=draft,
                )
            except Exception as exc:  # noqa: BLE001 — any failure needs to be reported back to the user
                logger.exception("Error extracting device data")
                result = DocIngestionResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=False,
                    error=str(exc),
                )

        await _orchestrator_client.post("/internal/doc-ingestion/result", json=result.model_dump())
    except Exception:
        logger.exception("Unrecoverable error processing a doc-ingestion request")


async def handle_extract(request: web.Request) -> web.Response:
    body = await request.json()
    doc_request = DocIngestionRequest.model_validate(body)

    task = asyncio.create_task(_process(doc_request))
    # Strong reference until it's done — otherwise the event loop could
    # garbage-collect the task mid-flight (asyncio docs, "Important").
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return web.json_response({"accepted": True}, status=202)


async def _on_config_change(changed_keys: set[str]) -> None:
    # asyncio.Semaphore doesn't let you change its limit after creation, so if
    # maxConcurrency changes we have to recreate it. Accepted simplification:
    # tasks already running on the old semaphore aren't migrated, so there's a
    # brief window where effective concurrency can exceed the new limit until
    # those finish — fine for a home project, not critical.
    if "maxConcurrency" in changed_keys:
        global _max_concurrency, _semaphore
        _max_concurrency = appconfig.get("maxConcurrency", 2)
        _semaphore = asyncio.Semaphore(_max_concurrency)
        logger.info("Max concurrency updated to %s (semaphore recreated)", _max_concurrency)


async def on_startup(app: web.Application) -> None:
    logger.info("doc-ingestion-worker starting up (max concurrency: %s)", _max_concurrency)
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
    app.router.add_post("/extract", handle_extract)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
