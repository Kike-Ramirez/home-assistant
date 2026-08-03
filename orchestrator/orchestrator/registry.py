"""Device registry access via PostgREST (schema `home`) — the read/write
surface the agent loop's tools call into (`orchestrator/tools.py`).

"Which device is relevant" is never filtered with our own heuristics —
Claude calls `list_devices` itself when it needs the inventory, and decides
which one a question is about.
"""

from __future__ import annotations

from typing import Any

from shared.postgrest_client import PostgrestClient

_DEVICE_COLUMNS = "id,display_name,brand,model,attributes,isa95_area"
_DEVICE_COLUMNS_WITH_STANDARDS = f"{_DEVICE_COLUMNS},device_standard(standard(code,name))"


async def get_or_create_device_type(pg: PostgrestClient, code: str, name: str) -> dict[str, Any]:
    return await pg.upsert_on_conflict(
        "device_type",
        {"code": code, "name": name},
        on_conflict="tenant_id,code",
    )


async def create_device(pg: PostgrestClient, draft: dict[str, Any]) -> dict[str, Any]:
    device_type = await get_or_create_device_type(
        pg,
        draft["device_type_code"],
        draft.get("device_type_name", draft["device_type_code"]),
    )
    return await pg.insert(
        "device",
        {
            "device_type_id": device_type["id"],
            "display_name": draft["display_name"],
            "brand": draft.get("brand"),
            "model": draft.get("model"),
            "attributes": draft.get("attributes", {}),
            "isa95_area": draft.get("isa95_area"),
        },
    )


async def update_device(pg: PostgrestClient, device_id: str, **fields: Any) -> dict[str, Any]:
    """Patches only the fields Claude actually supplied — e.g. `update_device(pg,
    device_id, brand="Bosch")` leaves everything else untouched. `None` values
    are dropped rather than sent, since PostgREST would otherwise happily
    overwrite a field with NULL when the caller just meant "not specified"."""
    payload = {k: v for k, v in fields.items() if v is not None}
    return await pg.patch("device", {"id": f"eq.{device_id}"}, payload)


async def list_devices(pg: PostgrestClient, include_standards: bool = False) -> list[dict[str, Any]]:
    """Uses PostgREST's resource embedding through the `device_standard` bridge
    table (requires the FKs already defined in db/schema.sql) when standards
    are requested."""
    columns = _DEVICE_COLUMNS_WITH_STANDARDS if include_standards else _DEVICE_COLUMNS
    return await pg.select("device", {"select": columns, "status": "eq.active"})


async def add_device_document(
    pg: PostgrestClient,
    device_id: str,
    kind: str,
    url_or_ref: str,
    description: str | None = None,
) -> dict[str, Any]:
    return await pg.insert(
        "device_document",
        {"device_id": device_id, "kind": kind, "url_or_ref": url_or_ref, "description": description},
    )


async def get_or_create_app_user(pg: PostgrestClient, channel: str, channel_user_id: str) -> dict[str, Any]:
    """Same upsert-on-conflict pattern as `conversation.get_or_create_conversation`.
    Called from the `schedule_reminder` tool — the first time anyone schedules
    a reminder, this closes the previously-open gap of nothing ever creating
    `home.app_user` rows (CLAUDE.md known gap #3)."""
    return await pg.upsert_on_conflict(
        "app_user",
        {"channel": channel, "channel_user_id": channel_user_id},
        on_conflict="tenant_id,channel,channel_user_id",
    )
