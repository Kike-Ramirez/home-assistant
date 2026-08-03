"""Fetches, dispatches, and reschedules reminders (home.reminder via PostgREST).

Moved here from notifier-scheduler as part of the "orchestrator owns every
external connection" redesign: notifier-scheduler no longer touches PostgREST
or MQTT directly, it just pings `POST /internal/reminders/check` on a timer
(see notifier-scheduler/README.md and CLAUDE.md).

Dispatch rule (CLAUDE.md section 4): if the reminder already carries a
ready-to-send deterministic message (`payload.message`), it goes straight to
the user's channel; otherwise, Claude words it (`claude_client.word_reminder`)
before sending — this used to be a dead end (an MQTT event nobody consumed);
now that orchestrator has its own Claude client, it's just a function call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from dateutil.rrule import rrulestr
from shared.message import MessageType, NormalizedMessage, outbound_topic
from shared.mqtt_client import ManagedMqttConnection
from shared.postgrest_client import PostgrestClient

from .claude_client import word_reminder

logger = logging.getLogger("orchestrator")


async def fetch_due_reminders(pg: PostgrestClient) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    return await pg.select("reminder", {"status": "eq.pending", "scheduled_at": f"lte.{now}"})


async def _find_user_channel(pg: PostgrestClient, user_id: str | None) -> tuple[str, str] | None:
    if not user_id:
        return None
    rows = await pg.select("app_user", {"id": f"eq.{user_id}"})
    if not rows:
        return None
    return rows[0]["channel"], rows[0]["channel_user_id"]


async def dispatch_reminder(pg: PostgrestClient, mqtt: ManagedMqttConnection, reminder: dict[str, Any]) -> None:
    payload = reminder.get("payload") or {}
    message_text = payload.get("message")
    user = await _find_user_channel(pg, reminder.get("user_id"))

    if not user:
        logger.warning("Reminder %s has no resolvable user/channel — skipping", reminder.get("id"))
        return

    channel, channel_user_id = user
    if not message_text:
        message_text = await word_reminder(reminder.get("kind", "maintenance"), payload)

    outgoing = NormalizedMessage(
        channel=channel,
        user_id=channel_user_id,
        conversation_id=channel_user_id,
        type=MessageType.TEXT,
        content=message_text,
    )
    await mqtt.publish(outbound_topic(channel, channel_user_id), outgoing.model_dump_json(), qos=1)


async def mark_sent(pg: PostgrestClient, reminder_id: str) -> None:
    await pg.patch(
        "reminder",
        {"id": f"eq.{reminder_id}"},
        {"status": "sent", "sent_at": datetime.now(timezone.utc).isoformat()},
    )


async def reschedule_or_mark_sent(pg: PostgrestClient, reminder: dict[str, Any]) -> None:
    rrule = reminder.get("recurrence_rule")
    if not rrule:
        await mark_sent(pg, reminder["id"])
        return

    scheduled_at = datetime.fromisoformat(reminder["scheduled_at"])
    rule = rrulestr(rrule, dtstart=scheduled_at)
    next_at = rule.after(datetime.now(timezone.utc))

    if next_at is None:
        # RRULE exhausted (e.g. COUNT/UNTIL reached) — treat it as done.
        await mark_sent(pg, reminder["id"])
        return

    await pg.patch("reminder", {"id": f"eq.{reminder['id']}"}, {"scheduled_at": next_at.isoformat()})


async def check_reminders(pg: PostgrestClient, mqtt: ManagedMqttConnection) -> int:
    """Runs one check cycle: fetch due reminders, dispatch each, reschedule or
    mark sent. Returns how many were processed (for the caller to log)."""
    due = await fetch_due_reminders(pg)
    for reminder in due:
        try:
            await dispatch_reminder(pg, mqtt, reminder)
            await reschedule_or_mark_sent(pg, reminder)
        except Exception:
            logger.exception("Error processing reminder %s", reminder.get("id"))
    return len(due)
