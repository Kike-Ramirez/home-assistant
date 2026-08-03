"""doc-ingestion-worker: consumer of home/events/doc_ingestion with bounded concurrency.

Doesn't block the normal chat flow (this can take a while) — CLAUDE.md section 4.
"""

from __future__ import annotations

import asyncio
import logging

from shared.message import DOC_INGESTION_REQUEST_TOPIC, DOC_INGESTION_RESULT_TOPIC, DocIngestionRequest, DocIngestionResult
from shared.mqtt_client import maintain_mqtt_connection
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, mqtt_secrets, system
from .extractor import extract_device_data

logger = logging.getLogger("doc_ingestion_worker")

_max_concurrency = appconfig.get("maxConcurrency", 2)
_semaphore = asyncio.Semaphore(_max_concurrency)


async def _process(client, payload: bytes) -> None:
    # The whole body is covered by the try — including the final publish, so
    # a network failure while replying doesn't get lost as an orphaned
    # exception in a fire-and-forget task.
    try:
        request = DocIngestionRequest.model_validate_json(payload)
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

        await client.publish(DOC_INGESTION_RESULT_TOPIC, payload=result.model_dump_json(), qos=1)
    except Exception:
        logger.exception("Unrecoverable error processing a doc-ingestion request")


_background_tasks: set[asyncio.Task] = set()


async def on_connect(client) -> None:
    await client.subscribe(DOC_INGESTION_REQUEST_TOPIC)
    async for message in client.messages:
        task = asyncio.create_task(_process(client, message.payload))
        # Strong reference until it's done — otherwise the event loop could
        # garbage-collect the task mid-flight (asyncio docs, "Important").
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


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


async def main() -> None:
    logger.info("doc-ingestion-worker starting up (max concurrency: %s)", _max_concurrency)
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig, on_change=_on_config_change))
    try:
        await maintain_mqtt_connection(mqtt_secrets, system, on_connect)
    finally:
        config_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
