"""Thin wrapper over aiomqtt for publishing/consuming the normalized message contract."""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import aiomqtt

from .settings import MqttSecrets, SystemConfig

logger = logging.getLogger(__name__)


@asynccontextmanager
async def mqtt_client(secrets: MqttSecrets) -> AsyncIterator[aiomqtt.Client]:
    tls_context = ssl.create_default_context() if secrets.tls_enabled else None
    async with aiomqtt.Client(
        hostname=secrets.host,
        port=secrets.port,
        username=secrets.user,
        password=secrets.password,
        tls_context=tls_context,
    ) as client:
        yield client


async def maintain_mqtt_connection(
    secrets: MqttSecrets,
    system: SystemConfig,
    on_connect: Callable[[aiomqtt.Client], Awaitable[None]],
) -> None:
    """Keeps an MQTT connection alive indefinitely.

    Every time there's a connection (new or reconnected), `on_connect(client)`
    is called — it normally subscribes to whatever it needs and iterates
    `client.messages`, which blocks until the connection drops. When that
    happens (or when a connection attempt fails), a warning is logged and the
    loop retries after `system.connect_timeout_seconds`.

    `system` is passed in (not a float computed once) so that if
    `connectTimeoutMs` changes live via `watch_appconfig` while this loop is
    already waiting, the next retry picks up the new value — a float captured
    once wouldn't notice the change.
    """
    while True:
        try:
            async with mqtt_client(secrets) as client:
                logger.info("Connected to MQTT (%s:%s)", secrets.host, secrets.port)
                await on_connect(client)
        except aiomqtt.MqttError as exc:
            timeout = system.connect_timeout_seconds
            logger.warning(
                "MQTT connection to %s:%s lost or failed (%s) — retrying in %.1fs. "
                "If this keeps happening, check that the broker is reachable and that "
                "MQTT_USER/MQTT_PASSWORD are correct.",
                secrets.host,
                secrets.port,
                exc,
                timeout,
            )
            await asyncio.sleep(timeout)


class ManagedMqttConnection:
    """Keeps track of the latest live MQTT connection so it can be used to
    publish on demand from outside the consume loop (e.g. an HTTP/Telegram
    handler that fires at any time), logging a warning if something tries to
    publish with no active connection — avoids duplicating that guard in
    every channel adapter.
    """

    def __init__(self, logger_name: str) -> None:
        self._client: aiomqtt.Client | None = None
        self._logger = logging.getLogger(logger_name)

    async def publish(self, topic: str, payload: str, qos: int = 1) -> None:
        if self._client is None:
            self._logger.warning("MQTT isn't available right now — message to %s dropped", topic)
            return
        await self._client.publish(topic, payload=payload, qos=qos)

    async def consume(
        self,
        client: aiomqtt.Client,
        topic_filter: str,
        handle_message: Callable[[aiomqtt.Message], Awaitable[None]],
    ) -> None:
        """Meant to be used as the `on_connect` callback for
        `maintain_mqtt_connection`: subscribes to `topic_filter`, exposes
        `client` to `publish()` for as long as the connection lasts, and
        dispatches each message to `handle_message` (a failure handling one
        message gets logged and doesn't stop consumption).
        """
        self._client = client
        try:
            await client.subscribe(topic_filter)
            async for message in client.messages:
                try:
                    await handle_message(message)
                except Exception:
                    self._logger.exception("Error processing message from %s", message.topic)
        finally:
            self._client = None
