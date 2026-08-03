"""notifier-scheduler: a cron heartbeat, nothing more.

Used to fetch due reminders from PostgREST and publish alerts to MQTT itself.
Under the "orchestrator owns every external connection" redesign, all of that
logic moved to orchestrator (see orchestrator/orchestrator/reminders.py) —
this service's only remaining job is to ping
`POST /internal/reminders/check` on a timer. It still exists as its own
service (rather than just being an APScheduler job inside orchestrator)
because of the one thing it still owns: its own Postgres-backed jobstore, so
scheduled ticks survive a container restart independently of orchestrator's
own process lifecycle.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from shared.internal_client import InternalApiClient
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, orchestrator_secrets, scheduler_secrets, system

logger = logging.getLogger("notifier_scheduler")

_orchestrator_client = InternalApiClient(orchestrator_secrets.url, SERVICE_NAME)


async def check_reminders() -> None:
    response = await _orchestrator_client.post("/internal/reminders/check", json={})
    if response is not None:
        logger.info("Reminder check completed: %s", response.json())
    # If it's None, InternalApiClient already logged the failure after its own
    # retries — nothing more to do, the next scheduled tick will try again.


async def main() -> None:
    check_interval = appconfig.get("checkIntervalSeconds", 300)
    logger.info(
        "notifier-scheduler starting up (check interval: %ss, orchestrator: %s)",
        check_interval,
        orchestrator_secrets.url,
    )
    scheduler = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=scheduler_secrets.postgres_dsn)})

    async def _on_config_change(changed_keys: set[str]) -> None:
        # Unlike most parameters (read fresh on every use), the APScheduler
        # job's interval is fixed once the trigger is created — if it
        # changes, it has to be rescheduled explicitly.
        if "checkIntervalSeconds" in changed_keys:
            new_interval = appconfig.get("checkIntervalSeconds", 300)
            scheduler.reschedule_job("check_reminders", trigger="interval", seconds=new_interval)
            logger.info("Check interval updated to %ss (job rescheduled)", new_interval)

    try:
        scheduler.add_job(
            check_reminders,
            trigger="interval",
            seconds=check_interval,
            id="check_reminders",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("notifier-scheduler started successfully")
        config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig, on_change=_on_config_change))
        try:
            await asyncio.Event().wait()  # keeps the process alive
        finally:
            config_task.cancel()
    except Exception:
        logger.exception("Unrecoverable error in notifier-scheduler — check POSTGRES_DSN (jobstore)")
        raise
    finally:
        scheduler.shutdown(wait=False)
        await _orchestrator_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
