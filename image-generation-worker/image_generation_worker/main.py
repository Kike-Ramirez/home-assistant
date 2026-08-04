"""image-generation-worker: gets a real picture for the `generate_image`
tool. Tries an actual web image search first (search.py), falls back to
Gemini image generation (generate.py) when that finds nothing usable, then
always normalizes the result to JPEG (convert.py) before calling back.

Mirrors doc-ingestion-worker/doc-generation-worker's shape: no MQTT, no
PostgREST — reached via `POST /generate-image`, calls back via
`POST /internal/image/result`.
"""

from __future__ import annotations

import base64
import re

from aiohttp import web
from shared.message import ImageRequest, ImageResult
from shared.worker_base import JobRunner

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .convert import to_jpeg
from .generate import generate_image
from .search import search_image


def _safe_filename(filename: str) -> str:
    """Sanitizes the model's suggested filename into something
    filesystem/Telegram-safe: ASCII words joined with underscores, always
    ending in `.jpg` regardless of what the model actually wrote there."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", filename).strip("_").lower()
    return (slug or "imagen") + ".jpg"


async def _run_job(request: ImageRequest) -> ImageResult:
    raw = await search_image(request.query)
    source = "search"
    if raw is None:
        raw = await generate_image(request.query)
        source = "generated"
    data = to_jpeg(raw)
    return ImageResult(
        conversation_id=request.conversation_id,
        channel_conversation_id=request.channel_conversation_id,
        channel=request.channel,
        user_id=request.user_id,
        success=True,
        filename=_safe_filename(request.filename),
        source=source,
        data_base64=base64.b64encode(data).decode("ascii"),
    )


def _build_failure_result(request: ImageRequest, exc: Exception) -> ImageResult:
    return ImageResult(
        conversation_id=request.conversation_id,
        channel_conversation_id=request.channel_conversation_id,
        channel=request.channel,
        user_id=request.user_id,
        success=False,
        error=str(exc),
    )


_runner = JobRunner(
    service_name=SERVICE_NAME,
    appconfig=appconfig,
    orchestrator_url=orchestrator_secrets.url,
    callback_path="/internal/image/result",
    request_model=ImageRequest,
    run_job=_run_job,
    build_failure_result=_build_failure_result,
    failure_log_message=(
        "Error getting an image — common causes: GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX not "
        "configured or its quota exhausted (falls back to Gemini generation either way, so "
        "this alone shouldn't fail the request), or the Gemini image model rejected the "
        "prompt/hit a quota limit."
    ),
    unrecoverable_log_message="Unrecoverable error processing an image request",
    ready_log_message="image-generation-worker ready (max concurrency: %s).",
)


def build_app() -> web.Application:
    return _runner.build_app("/generate-image", SERVICE_NAME, system)


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
