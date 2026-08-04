"""image-generation-worker: gets a real picture for the `generate_image`
tool. Tries an actual web image search first (search.py), falls back to
Gemini image generation (generate.py) when that finds nothing usable, then
always normalizes the result to JPEG (convert.py) before calling back.

Mirrors doc-ingestion-worker/doc-generation-worker's shape: no MQTT, no
PostgREST — reached via `POST /generate-image`, calls back via
`POST /internal/image/result`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re

from aiohttp import web
from shared.internal_client import InternalApiClient
from shared.message import ImageRequest, ImageResult
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .convert import to_jpeg
from .generate import generate_image
from .search import search_image

logger = logging.getLogger("image_generation_worker")

_max_concurrency = appconfig.get("maxConcurrency", 2)
_semaphore = asyncio.Semaphore(_max_concurrency)
_orchestrator_client = InternalApiClient(orchestrator_secrets.url, SERVICE_NAME)

_background_tasks: set[asyncio.Task] = set()


def _safe_filename(filename: str) -> str:
    """Sanitizes the model's suggested filename into something
    filesystem/Telegram-safe: ASCII words joined with underscores, always
    ending in `.jpg` regardless of what the model actually wrote there."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", filename).strip("_").lower()
    return (slug or "imagen") + ".jpg"


async def _process(request: ImageRequest) -> None:
    try:
        async with _semaphore:
            try:
                raw = await search_image(request.query)
                source = "search"
                if raw is None:
                    raw = await generate_image(request.query)
                    source = "generated"
                data = to_jpeg(raw)
                result = ImageResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=True,
                    filename=_safe_filename(request.filename),
                    source=source,
                    data_base64=base64.b64encode(data).decode("ascii"),
                )
            except Exception as exc:  # noqa: BLE001 — any failure needs to be reported back to the user
                logger.exception(
                    "Error getting an image — common causes: GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX not "
                    "configured or its quota exhausted (falls back to Gemini generation either way, so "
                    "this alone shouldn't fail the request), or the Gemini image model rejected the "
                    "prompt/hit a quota limit."
                )
                result = ImageResult(
                    conversation_id=request.conversation_id,
                    channel_conversation_id=request.channel_conversation_id,
                    channel=request.channel,
                    user_id=request.user_id,
                    success=False,
                    error=str(exc),
                )

        await _orchestrator_client.post("/internal/image/result", json=result.model_dump())
    except Exception:
        logger.exception("Unrecoverable error processing an image request")


async def handle_generate_image(request: web.Request) -> web.Response:
    body = await request.json()
    image_request = ImageRequest.model_validate(body)

    task = asyncio.create_task(_process(image_request))
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
    logger.info("image-generation-worker ready (max concurrency: %s).", _max_concurrency)
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
    app.router.add_post("/generate-image", handle_generate_image)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
