"""Thin HTTP client for service-to-service calls on the internal Docker
network (orchestrator's internal API, doc-ingestion-worker's `/extract`).

Not MQTT: these are direct request/response calls between two services that
both need to be up at the same moment (fire off a job, deliver a result,
trigger a periodic check) — a good fit for plain HTTP instead of forcing
everything through the bus. Bounded retry with a fixed backoff instead of
`maintain_mqtt_connection`'s retry-forever: unlike a persistent connection,
each of these is a one-off request tied to something concrete happening right
now (a user waiting for a reply, a scheduled tick) — after a few failed
attempts, the right move is to degrade gracefully (tell the user, or just
wait for the next scheduled tick) rather than hang indefinitely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx


class InternalApiClient:
    def __init__(
        self,
        base_url: str,
        service_name: str,
        max_retries: int = 2,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._logger = logging.getLogger(service_name)
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds

    async def post(self, path: str, json: dict[str, Any]) -> httpx.Response | None:
        """POSTs with bounded retry. Returns `None` (after logging the failure)
        if every attempt fails — callers should degrade gracefully rather than
        raise, same "never crash over a connection problem" policy as the rest
        of the project.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(path, json=json)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._logger.warning(
                        "Call to %s failed (%s) — retrying in %.1fs", path, exc, self._retry_delay_seconds
                    )
                    await asyncio.sleep(self._retry_delay_seconds)
        self._logger.error("Call to %s failed after %d attempts: %s", path, self._max_retries + 1, last_exc)
        return None

    async def aclose(self) -> None:
        await self._client.aclose()
