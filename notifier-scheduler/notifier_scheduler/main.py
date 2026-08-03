"""notifier-scheduler: checks pending reminders and publishes alerts to the bus.

Cron-like via APScheduler. Unlike the rest of the services here, this one
doesn't keep a persistent MQTT connection: it only ever publishes (never
consumes/subscribes to anything), so each scheduled run opens a fresh
connection, publishes whatever's due, and closes it — simpler than managing
the lifecycle of a persistent connection that would sit idle most of the time.
`connectTimeoutMs` is used here as the wait before a single retry if the
connection attempt fails; if it fails again, it's left for the next scheduler
tick.
"""

from __future__ import annotations

import asyncio
import logging

import aiomqtt
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.mqtt_client import mqtt_client
from shared.postgrest_client import PostgrestClient
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, mqtt_secrets, postgrest_secrets, scheduler_secrets, system
from .reminders import dispatch_reminder, fetch_due_reminders, reschedule_or_mark_sent

logger = logging.getLogger("notifier_scheduler")


async def _dispatch_all(pg: PostgrestClient, due: list[dict]) -> None:
    for attempt in (1, 2):
        try:
            async with mqtt_client(mqtt_secrets) as client:
                for reminder in due:
                    try:
                        await dispatch_reminder(pg, client, reminder)
                        await reschedule_or_mark_sent(pg, reminder)
                    except Exception:
                        logger.exception("Error processing reminder %s", reminder.get("id"))
            return
        except aiomqtt.MqttError as exc:
            if attempt == 1:
                logger.warning(
                    "MQTT connection failed (%s) — retrying in %.1fs", exc, system.connect_timeout_seconds
                )
                await asyncio.sleep(system.connect_timeout_seconds)
            else:
                logger.error("Couldn't connect to MQTT after retrying — will try again next cycle")


async def check_reminders(pg: PostgrestClient) -> None:
    try:
        due = await fetch_due_reminders(pg)
    except Exception:
        logger.exception("Error fetching pending reminders")
        return

    if not due:
        logger.debug("No pending reminders this cycle")
        return

    await _dispatch_all(pg, due)


async def main() -> None:
    check_interval = appconfig.get("checkIntervalSeconds", 300)
    logger.info(
        "notifier-scheduler starting up (check interval: %ss, PostgREST: %s)",
        check_interval,
        postgrest_secrets.url,
    )
    pg = PostgrestClient(postgrest_secrets.url)
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
            args=[pg],
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
        logger.exception(
            "Unrecoverable error in notifier-scheduler — check POSTGRES_DSN (jobstore) and connectivity to PostgREST"
        )
        raise
    finally:
        scheduler.shutdown(wait=False)
        await pg.aclose()


if __name__ == "__main__":
    asyncio.run(main())
