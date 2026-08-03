"""Fetches, dispatches, and reschedules reminders (home.reminder via PostgREST).

Dispatch rule (CLAUDE.md section 4): if the reminder already carries a
ready-to-send deterministic message (`payload.message`), it goes straight to
the user's channel; otherwise, an event (`home/events/<kind>`) is published so
the orchestrator can word it with Claude before sending it — that consumer
side on the orchestrator is future work, not part of this skeleton (see the
note in CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiomqtt
from dateutil.rrule import rrulestr

from shared.message import (
    FIRMWARE_UPDATE_EVENT_TOPIC,
    PRICE_ALERT_EVENT_TOPIC,
    REMINDER_EVENT_TOPIC,
    MessageType,
    NormalizedMessage,
    outbound_topic,
)
from shared.postgrest_client import PostgrestClient

logger = logging.getLogger("notifier_scheduler")

_KIND_TOPIC = {
    "maintenance": REMINDER_EVENT_TOPIC,
    "price_alert": PRICE_ALERT_EVENT_TOPIC,
    "firmware_update": FIRMWARE_UPDATE_EVENT_TOPIC,
}


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


async def dispatch_reminder(pg: PostgrestClient, mqtt: aiomqtt.Client, reminder: dict[str, Any]) -> None:
    payload = reminder.get("payload") or {}
    message_text = payload.get("message")
    user = await _find_user_channel(pg, reminder.get("user_id"))

    if message_text and user:
        channel, channel_user_id = user
        outgoing = NormalizedMessage(
            channel=channel,
            user_id=channel_user_id,
            conversation_id=channel_user_id,
            type=MessageType.TEXT,
            content=message_text,
        )
        await mqtt.publish(outbound_topic(channel, channel_user_id), payload=outgoing.model_dump_json(), qos=1)
        return

    # No deterministic message (or the user couldn't be resolved): publish
    # the raw event so something downstream (orchestrator, eventually)
    # decides how to word it and who to send it to.
    topic = _KIND_TOPIC.get(reminder.get("kind", ""), REMINDER_EVENT_TOPIC)
    await mqtt.publish(topic, payload=json.dumps(reminder), qos=1)


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
