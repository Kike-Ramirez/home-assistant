"""Shared scaffold for the three "accept a job over HTTP, run it under
bounded concurrency, POST the result back to orchestrator" workers
(doc-ingestion-worker, doc-generation-worker, image-generation-worker) —
factors out everything that used to be identical, hand-copied boilerplate
across their three `main.py` files: the semaphore lifecycle, the
background-task strong-reference set (asyncio docs, "Important" — otherwise
the event loop can garbage-collect an in-flight task), the outer
try/except-then-callback-POST wrapper, `maxConcurrency` hot-reload, and the
aiohttp app wiring.

Each worker still owns 100% of its own domain logic — vision extraction,
PDF/CSV rendering, image search+generation+JPEG conversion — as a plain
`run_job(request) -> BaseModel` async callable it hands to `JobRunner`; this
module never sees or cares what that callable actually does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiohttp import web
from pydantic import BaseModel

from .internal_client import InternalApiClient
from .settings import ConfigAccessor, SystemConfig, watch_appconfig

RequestT = TypeVar("RequestT", bound=BaseModel)

RunJob = Callable[[RequestT], Awaitable[BaseModel]]
BuildFailureResult = Callable[[RequestT, Exception], BaseModel]


class JobRunner:
    """One instance per worker process — everything that's identical across
    the three workers' `main.py` (see module docstring). Construct one in
    each worker's `main.py`, pass it `run_job`/`build_failure_result` for the
    domain-specific part, and call `.build_app(...)` for the aiohttp wiring.
    """

    def __init__(
        self,
        *,
        service_name: str,
        appconfig: ConfigAccessor,
        orchestrator_url: str,
        callback_path: str,
        request_model: type[RequestT],
        run_job: RunJob,
        build_failure_result: BuildFailureResult,
        failure_log_message: str,
        unrecoverable_log_message: str,
        ready_log_message: str,
    ) -> None:
        self._logger = logging.getLogger(service_name)
        self._appconfig = appconfig
        self._max_concurrency = appconfig.get("maxConcurrency", 2)
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._orchestrator_client = InternalApiClient(orchestrator_url, service_name)
        self._background_tasks: set[asyncio.Task] = set()
        self._callback_path = callback_path
        self._request_model = request_model
        self._run_job = run_job
        self._build_failure_result = build_failure_result
        self._failure_log_message = failure_log_message
        self._unrecoverable_log_message = unrecoverable_log_message
        self._ready_log_message = ready_log_message

    async def _process(self, request: Any) -> None:
        # The whole body is covered by the try — including the final callback, so
        # a network failure while replying doesn't get lost as an orphaned
        # exception in a fire-and-forget task.
        try:
            async with self._semaphore:
                try:
                    result = await self._run_job(request)
                except Exception as exc:  # noqa: BLE001 — any failure needs to be reported back to the user
                    self._logger.exception(self._failure_log_message)
                    result = self._build_failure_result(request, exc)

            await self._orchestrator_client.post(self._callback_path, json=result.model_dump())
        except Exception:
            self._logger.exception(self._unrecoverable_log_message)

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        job_request = self._request_model.model_validate(body)

        task = asyncio.create_task(self._process(job_request))
        # Strong reference until it's done — otherwise the event loop could
        # garbage-collect the task mid-flight (asyncio docs, "Important").
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"accepted": True}, status=202)

    async def _on_config_change(self, changed_keys: set[str]) -> None:
        # asyncio.Semaphore doesn't let you change its limit after creation, so if
        # maxConcurrency changes we have to recreate it. Accepted simplification:
        # tasks already running on the old semaphore aren't migrated, so there's a
        # brief window where effective concurrency can exceed the new limit until
        # those finish — fine for a home project, not critical.
        if "maxConcurrency" in changed_keys:
            self._max_concurrency = self._appconfig.get("maxConcurrency", 2)
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
            self._logger.info("Max concurrency updated to %s (semaphore recreated)", self._max_concurrency)

    def build_app(self, route_path: str, service_name: str, system: SystemConfig) -> web.Application:
        app = web.Application()

        async def on_startup(app: web.Application) -> None:
            self._logger.info(self._ready_log_message, self._max_concurrency)
            app["config_task"] = asyncio.create_task(
                watch_appconfig(service_name, system, self._appconfig, on_change=self._on_config_change)
            )

        async def on_shutdown(app: web.Application) -> None:
            app["config_task"].cancel()
            await self._orchestrator_client.aclose()

        app.on_startup.append(on_startup)
        app.on_shutdown.append(on_shutdown)
        app.router.add_post(route_path, self.handle)
        return app
