"""The tools the active LLM engine can call during the agent loop
(`shared.engines.Engine.run_agent_loop` — see `shared/shared/engines/`).

Every side-effecting action the model decides on — saving a device, editing
one, attaching documentation, scheduling a reminder — goes through one of
these. Orchestrator itself makes no decisions about *when* to call them; it
only validates shapes (via the JSON schemas below) and executes what the
model asks for (CLAUDE.md section 10). `TOOL_SCHEMAS` is plain JSON Schema —
the same list works unchanged regardless of which engine is active.

`extract_device_data` is deliberately NOT handled by `dispatch()` below — it's
the one tool with real async work behind it (doc-ingestion-worker's
fire-and-forget `/extract`), so `orchestrator/main.py` kicks it off directly
when the loop pauses and injects the result back on resume (see
`ASYNC_TOOL_NAMES` and `Engine.run_agent_loop`'s `async_tool_names`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.postgrest_client import PostgrestClient

from . import registry

ASYNC_TOOL_NAMES = frozenset({"extract_device_data"})

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_devices",
        "description": (
            "Returns the household's device inventory. Call this whenever you need to know what "
            "devices exist before answering — troubleshooting, replacement/purchase recommendations, "
            "checking whether a device already exists, or general questions about the home."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_standards": {
                    "type": "boolean",
                    "description": "Set true when you need each device's supported standards/protocols "
                    "(e.g. Zigbee, Matter) — needed for compatibility/replacement recommendations.",
                }
            },
        },
    },
    {
        "name": "create_device",
        "description": (
            "Saves a NEW device to the household inventory. Call this once you have enough confirmed "
            "information to register it — after the user confirms a draft you extracted from a photo, "
            "or from a plain description they typed. Don't call this for a device that already exists — "
            "use update_device instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "description": "Short name to identify it by, e.g. brand + model."},
                "device_type_code": {
                    "type": "string",
                    "description": "snake_case, stable — the same kind of device must always get the same code.",
                },
                "device_type_name": {"type": "string"},
                "brand": {"type": "string"},
                "model": {"type": "string"},
                "isa95_area": {"type": "string", "description": "Room/location, e.g. 'kitchen', if known."},
                "attributes": {"type": "object", "description": "Any other spec worth remembering (capacity, voltage, ...)."},
            },
            "required": ["display_name", "device_type_code", "device_type_name"],
        },
    },
    {
        "name": "update_device",
        "description": (
            "Edits an EXISTING device already in the inventory — corrections ('actually it's a Bosch, "
            "not a Balay') or adding detail later. Only pass the fields that actually changed; "
            "get the device_id from list_devices first if you don't already have it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "display_name": {"type": "string"},
                "brand": {"type": "string"},
                "model": {"type": "string"},
                "isa95_area": {"type": "string"},
                "attributes": {"type": "object"},
            },
            "required": ["device_id"],
        },
    },
    {
        "name": "attach_document",
        "description": (
            "Attaches documentation to an EXISTING device — a manual, a label photo, or a free-form "
            "note. Use this when a photo/document/text the user sent is meant to be saved as reference "
            "material for a device they already have — not a new device to onboard (use "
            "extract_device_data + create_device for that instead)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["photo", "manual", "note"]},
                "url_or_data": {
                    "type": "string",
                    "description": "The attachment's URL or data URI, exactly as it appeared in this conversation.",
                },
                "description": {"type": "string"},
            },
            "required": ["device_id", "kind", "url_or_data"],
        },
    },
    {
        "name": "extract_device_data",
        "description": (
            "Analyzes a photo already in this conversation as a device label/manual and extracts "
            "structured data (brand, model, specs) as a starting draft for onboarding a NEW device. "
            "Only call this when the photo is genuinely a label/manual you intend to register as a "
            "device — not for an error message shown on a screen, a document to attach to an existing "
            "device, or anything else. This takes a moment; say a brief sentence before calling it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attachment_index": {
                    "type": "integer",
                    "description": "Which image in this message to analyze, if more than one was sent (0-indexed). Defaults to the first.",
                },
            },
        },
    },
    {
        "name": "schedule_reminder",
        "description": (
            "Creates a reminder that gets sent back to the user later — maintenance, a price check, a "
            "firmware check, or anything else they ask to be reminded about."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["maintenance", "price_alert", "firmware_update"]},
                "scheduled_at": {"type": "string", "description": "ISO 8601 timestamp for when to send it."},
                "message": {"type": "string", "description": "What to tell the user when it fires."},
                "device_id": {"type": "string", "description": "The device this reminder is about, if any."},
                "recurrence_rule": {"type": "string", "description": "An iCal RRULE if this should repeat, otherwise omit."},
            },
            "required": ["kind", "scheduled_at", "message"],
        },
    },
]

@dataclass
class ToolContext:
    """The bit of per-message context a tool needs beyond its own arguments —
    today, only `schedule_reminder` uses it (to resolve/create the app_user
    a reminder belongs to)."""

    channel: str
    channel_user_id: str  # the channel's native user id (msg.user_id) — e.g. a Telegram chat id


async def _schedule_reminder(pg: PostgrestClient, ctx: ToolContext, tool_input: dict[str, Any]) -> dict[str, Any]:
    user = await registry.get_or_create_app_user(pg, ctx.channel, ctx.channel_user_id)
    return await pg.insert(
        "reminder",
        {
            "user_id": user["id"],
            "device_id": tool_input.get("device_id"),
            "kind": tool_input["kind"],
            "payload": {"message": tool_input["message"]},
            "scheduled_at": tool_input["scheduled_at"],
            "recurrence_rule": tool_input.get("recurrence_rule"),
        },
    )


async def dispatch(pg: PostgrestClient, ctx: ToolContext, name: str, tool_input: dict[str, Any]) -> Any:
    if name == "list_devices":
        return await registry.list_devices(pg, include_standards=tool_input.get("include_standards", False))
    if name == "create_device":
        return await registry.create_device(pg, tool_input)
    if name == "update_device":
        device_id = tool_input["device_id"]
        fields = {k: v for k, v in tool_input.items() if k != "device_id"}
        return await registry.update_device(pg, device_id, **fields)
    if name == "attach_document":
        return await registry.add_device_document(
            pg, tool_input["device_id"], tool_input["kind"], tool_input["url_or_data"], tool_input.get("description")
        )
    if name == "schedule_reminder":
        return await _schedule_reminder(pg, ctx, tool_input)
    raise ValueError(f"Unknown or unsupported-here tool: {name!r}")


def make_executor(pg: PostgrestClient, ctx: ToolContext) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """A `shared.engines.ToolExecutor` closed over this turn's `pg`/`ctx` —
    factored out so `orchestrator/main.py` doesn't redefine the same closure
    at both of its call sites (the normal turn and the doc-ingestion resume)."""

    async def executor(name: str, tool_input: dict[str, Any]) -> Any:
        return await dispatch(pg, ctx, name, tool_input)

    return executor
