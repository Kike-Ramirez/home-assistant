"""Helpers for accessing the device inventory via PostgREST (schema `home`).

"Which device is relevant" is never filtered with our own heuristics — the
full inventory (usually just a handful of devices per home) gets passed to
Claude, and it decides, given the context of the question, which one the
user means.
"""

from __future__ import annotations

from typing import Any

from shared.postgrest_client import PostgrestClient


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


async def list_devices(pg: PostgrestClient) -> list[dict[str, Any]]:
    return await pg.select(
        "device",
        {"select": "id,display_name,brand,model,attributes,isa95_area", "status": "eq.active"},
    )


async def list_devices_with_standards(pg: PostgrestClient) -> list[dict[str, Any]]:
    """Same as `list_devices` but with the supported standards/protocols embedded.

    Uses PostgREST's resource embedding through the `device_standard` bridge
    table (requires the FKs already defined in db/schema.sql).
    """
    return await pg.select(
        "device",
        {
            "select": "id,display_name,brand,model,attributes,isa95_area,device_standard(standard(code,name))",
            "status": "eq.active",
        },
    )
