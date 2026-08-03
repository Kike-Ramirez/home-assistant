"""Conversation state in Postgres (via PostgREST) — never in process memory.

Needed for the orchestrator to be stateless and scale to replicas
(CLAUDE.md section 4).
"""

from __future__ import annotations

from typing import Any

from shared.postgrest_client import PostgrestClient


async def get_or_create_conversation(pg: PostgrestClient, channel: str, channel_conversation_id: str) -> dict[str, Any]:
    rows = await pg.select(
        "conversation",
        {
            "channel": f"eq.{channel}",
            "channel_conversation_id": f"eq.{channel_conversation_id}",
        },
    )
    if rows:
        return rows[0]
    # upsert (not a plain insert) — avoids a 409 if two messages from the same
    # chat arrive almost simultaneously and both try to create the
    # conversation first. merge-duplicates with state/status in the payload
    # won't clobber a row the other request already created, because the
    # unique index is (tenant_id, channel, channel_conversation_id) and both
    # requests send the same value — PostgREST does the upsert atomically at
    # the Postgres level.
    return await pg.upsert_on_conflict(
        "conversation",
        {
            "channel": channel,
            "channel_conversation_id": channel_conversation_id,
            "state": {},
            "status": "open",
        },
        on_conflict="tenant_id,channel,channel_conversation_id",
    )


async def get_conversation_by_id(pg: PostgrestClient, conversation_id: str) -> dict[str, Any]:
    rows = await pg.select("conversation", {"id": f"eq.{conversation_id}"})
    return rows[0]


async def update_state(pg: PostgrestClient, conversation_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return await pg.patch("conversation", {"id": f"eq.{conversation_id}"}, {"state": state})


def clear_keys(state: dict[str, Any], *keys: str) -> dict[str, Any]:
    """New dict without the given keys — used to close out a flow and put
    `conversation.state` back the way it was before it started (e.g. dropping
    `pending_action`/`draft_device` once device onboarding is done)."""
    return {k: v for k, v in state.items() if k not in keys}
