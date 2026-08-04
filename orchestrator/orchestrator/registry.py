"""Device registry access via PostgREST (schema `home`) — the read/write
surface the agent loop's tools call into (`orchestrator/actions.py`).

"Which device is relevant" is never filtered with our own heuristics —
Gemini calls `list_devices` itself when it needs the inventory, and decides
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
    """Patches only the fields Gemini actually supplied — e.g. `update_device(pg,
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
    "device_document(id,kind,url_or_ref,media_type,description,created_at)"
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


def _device_id_filter(device_id: str | None) -> str:
    """`device_id=None` means "the household in general", stored as a NULL
    column (db/schema.sql) — PostgREST's `is.null` operator, not `eq.`, is
    how that's actually filtered on."""
    return "is.null" if device_id is None else f"eq.{device_id}"


async def add_device_document(
    pg: PostgrestClient,
    device_id: str | None,
    kind: str,
    url_or_ref: str,
    description: str | None = None,
    media_type: str | None = None,
) -> dict[str, Any]:
    """`device_id=None` saves it as a general household document (not tied to
    any one device) instead of failing — used both by the `attach_document`
    tool and by `main.py`'s automatic persistence of anything sent to/from
    Gemini once it's associated with a device (or isn't associated with any
    one device in particular)."""
    return await pg.insert(
        "device_document",
        {
            "device_id": device_id,
            "kind": kind,
            "url_or_ref": url_or_ref,
            "description": description,
            "media_type": media_type,
        },
    )


async def list_house_documents(pg: PostgrestClient) -> list[dict[str, Any]]:
    """General household documents/images — attached or generated without
    being tied to any one specific device (a full-inventory report, an image
    that isn't about a particular device). The `device_id IS NULL` half of
    `device_document` that `get_device` doesn't cover."""
    return await pg.select(
        "device_document",
        {"select": "id,kind,url_or_ref,media_type,description,created_at", "device_id": _device_id_filter(None)},
    )


async def get_latest_device_photo(pg: PostgrestClient, device_id: str | None) -> dict[str, Any] | None:
    """The most recently attached `device_document` of kind 'photo' for this
    device (or for the household in general, if `device_id` is `None`), if
    any. Used by the `generate_image` tool to check for a real photo already
    on file before searching the web or generating one — `.select()` doesn't
    support `order`/`limit` (see postgrest_client.py), so with normally just a
    handful of photos per device, picking the newest in Python is simpler
    than extending that shared filter-only interface for one caller."""
    rows = await pg.select(
        "device_document",
        {
            "select": "id,url_or_ref,media_type,description,created_at",
            "device_id": _device_id_filter(device_id),
            "kind": "eq.photo",
        },
    )
    if not rows:
        return None
    return max(rows, key=lambda r: r["created_at"])
