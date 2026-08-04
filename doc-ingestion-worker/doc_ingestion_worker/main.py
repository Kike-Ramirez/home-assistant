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

from aiohttp import web
from shared.message import DocIngestionRequest, DocIngestionResult
from shared.worker_base import JobRunner

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, system
from .extractor import extract_device_data


async def _run_job(request: DocIngestionRequest) -> DocIngestionResult:
    draft = await extract_device_data(request.attachment_url)
    return DocIngestionResult(
        conversation_id=request.conversation_id,
        channel_conversation_id=request.channel_conversation_id,
        channel=request.channel,
        user_id=request.user_id,
        success=True,
        draft_device=draft,
    )


def _build_failure_result(request: DocIngestionRequest, exc: Exception) -> DocIngestionResult:
    return DocIngestionResult(
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
    callback_path="/internal/doc-ingestion/result",
    request_model=DocIngestionRequest,
    run_job=_run_job,
    build_failure_result=_build_failure_result,
    failure_log_message=(
        "Error extracting device data — common causes: attachment media_type unsupported by "
        "Gemini, GEMINI_API_KEY quota/rate-limit exhausted, or the attachment isn't reachable."
    ),
    unrecoverable_log_message="Unrecoverable error processing a doc-ingestion request",
    ready_log_message="doc-ingestion-worker ready (max concurrency: %s).",
)


def build_app() -> web.Application:
    return _runner.build_app("/extract", SERVICE_NAME, system)


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8080))
