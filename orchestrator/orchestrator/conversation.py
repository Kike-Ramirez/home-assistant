"""Conversation state in Postgres (via PostgREST) — never in process memory.

Needed for the orchestrator to be stateless and scale to replicas
(CLAUDE.md section 4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from shared.postgrest_client import PostgrestClient

logger = logging.getLogger("orchestrator")


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


async def update_state(
    pg: PostgrestClient, conversation: dict[str, Any], mutate: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """Applies `mutate` to `conversation`'s current state and writes the
    result back, guarded by optimistic concurrency — not just a blind PATCH
    of whatever the caller last read.

    Every call site used to build the *entire* new state from a state
    snapshot read once at the top of a handler (`{**conversation["state"],
    "history": ...}`), then PATCH the whole `state` column with no check that
    it hadn't changed since. With three worker callbacks plus normal inbound
    traffic all able to touch the same conversation, two concurrent
    read-modify-writes can silently clobber each other (a lost update) — the
    two race conditions already found and fixed this project (pending_agent_turn
    set *after* firing an async request, letting a fast callback beat the
    write) were specific instances of exactly this general class of bug.

    `mutate` receives whatever state is actually current at write time (the
    snapshot on the first attempt, a freshly re-read one on retry) and
    returns the new state — callers pass the same small transform they used
    to inline (e.g. `lambda state: {**state, "history": result.messages}`),
    not a state dict they built once and handed over stale.

    The guard: the PATCH also filters on `updated_at=eq.<value last seen>`,
    so it only applies if no one else has written this row since — and this
    always sets `updated_at` to now() itself in the same PATCH, since nothing
    else does (no DB trigger, no auto-touch): without that, `updated_at`
    would never actually change and the "last seen" comparison would be
    comparing a value against itself forever, making the guard a no-op. A
    zero-row result means someone else won — this re-fetches the conversation
    fresh, re-applies `mutate` on top of THAT, and writes once more (this
    second attempt is unconditional: at some point forward progress matters
    more than a vanishingly unlikely third collision, and callers don't have
    to thread a retry loop of their own).
    """
    new_state = mutate(conversation["state"])
    updated = await pg.patch_if_match(
        "conversation",
        {"id": f"eq.{conversation['id']}", "updated_at": f"eq.{conversation['updated_at']}"},
        {"state": new_state, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated is not None:
        return updated

    logger.warning(
        "Conversation %s changed concurrently — retrying this state update against the fresh row.",
        conversation["id"],
    )
    fresh = await get_conversation_by_id(pg, conversation["id"])
    return await pg.patch(
        "conversation",
        {"id": f"eq.{fresh['id']}"},
        {"state": mutate(fresh["state"]), "updated_at": datetime.now(timezone.utc).isoformat()},
    )


def clear_keys(state: dict[str, Any], *keys: str) -> dict[str, Any]:
    """New dict without the given keys — used to drop `pending_agent_turn`
    once a paused agent-loop turn resumes and completes."""
    return {k: v for k, v in state.items() if k not in keys}
