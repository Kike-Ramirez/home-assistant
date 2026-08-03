"""Device registry access via PostgREST (schema `home`) — the read/write
surface the agent loop's tools call into (`orchestrator/actions.py`).

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


async def retire_device(pg: PostgrestClient, device_id: str) -> dict[str, Any]:
    """Soft-delete: flips `status` to 'retired' so it drops out of
    `list_devices` (which filters `status=eq.active`) without erasing history."""
    return await pg.patch("device", {"id": f"eq.{device_id}"}, {"status": "retired"})


async def list_devices(pg: PostgrestClient, include_standards: bool = False) -> list[dict[str, Any]]:
    """Uses PostgREST's resource embedding through the `device_standard` bridge
    table (requires the FKs already defined in db/schema.sql) when standards
    are requested."""
    columns = _DEVICE_COLUMNS_WITH_STANDARDS if include_standards else _DEVICE_COLUMNS
    return await pg.select("device", {"select": columns, "status": "eq.active"})


_DEVICE_DETAIL_COLUMNS = (
    f"{_DEVICE_COLUMNS_WITH_STANDARDS},"
    "device_document(id,kind,url_or_ref,description,created_at)"
)


async def get_device(pg: PostgrestClient, device_id: str) -> dict[str, Any]:
    """Full detail for one device — standards and attached documents embedded,
    for troubleshooting/edit flows where the flat `list_devices` row isn't enough."""
    rows = await pg.select("device", {"select": _DEVICE_DETAIL_COLUMNS, "id": f"eq.{device_id}"})
    if not rows:
        raise ValueError(f"No device with id {device_id!r}")
    return rows[0]


async def get_compatible_devices(pg: PostgrestClient, device_id: str) -> Any:
    """Exposes the `home.compatible_devices` SQL function (db/schema.sql) via
    PostgREST's RPC endpoint — combines explicit device_compatibility entries
    with shared-standard matches."""
    return await pg.rpc("compatible_devices", {"p_device_id": device_id})


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
