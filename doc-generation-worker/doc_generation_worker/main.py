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

import base64

from aiohttp import web
from shared.message import DocGenerationRequest, DocGenerationResult
from shared.worker_base import JobRunner

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .renderer import EXTENSIONS, MEDIA_TYPES, render


async def _run_job(request: DocGenerationRequest) -> DocGenerationResult:
    data = render(request.file_type, request.content)
    filename = request.filename
    if not filename.endswith(EXTENSIONS[request.file_type]):
        filename += EXTENSIONS[request.file_type]
    return DocGenerationResult(
        conversation_id=request.conversation_id,
        channel_conversation_id=request.channel_conversation_id,
        channel=request.channel,
        user_id=request.user_id,
        success=True,
        filename=filename,
        media_type=MEDIA_TYPES[request.file_type],
        data_base64=base64.b64encode(data).decode("ascii"),
    )


def _build_failure_result(request: DocGenerationRequest, exc: Exception) -> DocGenerationResult:
    return DocGenerationResult(
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
    callback_path="/internal/doc-generation/result",
    request_model=DocGenerationRequest,
    run_job=_run_job,
    build_failure_result=_build_failure_result,
    failure_log_message=(
        "Error rendering a document — common cause: unexpected file_type/content in "
        "request.content — see render() in render.py for the exact format it expects."
    ),
    unrecoverable_log_message="Unrecoverable error processing a doc-generation request",
    ready_log_message="doc-generation-worker ready (max concurrency: %s).",
)


def build_app() -> web.Application:
    return _runner.build_app("/generate", SERVICE_NAME, system)


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
