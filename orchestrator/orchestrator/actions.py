"""The tools Gemini can call during the agent loop (`gemini_client.run_agent_loop`).

Every side-effecting action the model decides on — saving a device, editing
one, retiring one, attaching documentation — goes through one of these.
Orchestrator itself makes no decisions about *when* to call them; it only
validates shapes (via the JSON schemas below) and executes what the model
asks for (CLAUDE.md section 10).

Each tool is declared once, as a `(schema, handler)` pair in `_TOOLS` below —
`TOOL_SCHEMAS` (what Gemini sees) and the dispatch table (what actually runs)
are both derived from it, so a new tool can't have a schema with no handler
or vice versa; the assertion right after `_TOOLS` catches a typo in
`ASYNC_TOOL_NAMES`/`CONFIRM_TOOL_NAMES` at import time instead of at the
first real tool call.

Two tool sets change the agent loop's normal "call it immediately" behavior:

- `ASYNC_TOOL_NAMES` (`extract_device_data`, `generate_document`,
  `generate_image`): real async work behind them (doc-ingestion-worker's
  fire-and-forget `/extract`, doc-generation-worker's fire-and-forget
  `/generate`, image-generation-worker's fire-and-forget `/generate-image`)
  — `orchestrator/main.py` kicks the right one off directly when the loop
  pauses and injects the result back on resume.
- `CONFIRM_TOOL_NAMES` (`create_device`, `update_device`, `retire_device`):
  writes/destructive changes to the inventory — `orchestrator/main.py` pauses
  the loop the same way, but instead of doing async work it asks the user to
  approve/reject via the channel (Human-in-the-Loop, see `security_guard.py`)
  before actually calling `dispatch()`. Both sets are passed together as
  `run_agent_loop`'s `async_tool_names` — the model-facing pausing behavior
  is identical, only what `main.py` does while paused differs.

Read-only tools (`list_devices`, `get_device`, `get_compatible_devices`) and
the purely additive `attach_document` (adds a document reference, never
overwrites or destroys existing data) execute immediately, no approval needed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from shared.postgrest_client import PostgrestClient

from . import registry

ASYNC_TOOL_NAMES = frozenset({"extract_device_data", "generate_document", "generate_image"})
CONFIRM_TOOL_NAMES = frozenset({"create_device", "update_device", "retire_device"})

Handler = Callable[[PostgrestClient, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class _Tool:
    schema: dict[str, Any]
    handler: Handler

    @property
    def name(self) -> str:
        return self.schema["name"]


async def _list_devices(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.list_devices(pg, include_standards=tool_input.get("include_standards", False))


async def _get_device(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.get_device(pg, tool_input["device_id"])


async def _get_compatible_devices(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.get_compatible_devices(pg, tool_input["device_id"])


async def _create_device(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.create_device(pg, tool_input)


async def _update_device(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    device_id = tool_input["device_id"]
    fields = {k: v for k, v in tool_input.items() if k != "device_id"}
    return await registry.update_device(pg, device_id, **fields)


async def _retire_device(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.retire_device(pg, tool_input["device_id"])


async def _attach_document(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    return await registry.add_device_document(
        pg, tool_input["device_id"], tool_input["kind"], tool_input["url_or_data"], tool_input.get("description")
    )


async def _unreachable_extract_device_data(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    """`extract_device_data` is handled directly by `orchestrator/main.py` (the
    one entry in `ASYNC_TOOL_NAMES` with real async work behind it) — it's
    registered here only so it has a schema and a name; `dispatch()` must
    never actually be asked to run it."""
    raise AssertionError("extract_device_data must be handled by main.py, never dispatch()")


async def _unreachable_generate_document(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    """Same reasoning as `_unreachable_extract_device_data` — `generate_document`
    is the other entry in `ASYNC_TOOL_NAMES` with real async work behind it
    (doc-generation-worker's `/generate`), handled directly by `main.py`."""
    raise AssertionError("generate_document must be handled by main.py, never dispatch()")


async def _unreachable_generate_image(pg: PostgrestClient, tool_input: dict[str, Any]) -> Any:
    """Same reasoning as `_unreachable_extract_device_data` — `generate_image`
    is the third entry in `ASYNC_TOOL_NAMES` with real async work behind it
    (image-generation-worker's `/generate-image`), handled directly by `main.py`."""
    raise AssertionError("generate_image must be handled by main.py, never dispatch()")


_TOOLS: list[_Tool] = [
    _Tool(
        schema={
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
        handler=_list_devices,
    ),
    _Tool(
        schema={
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
        handler=_create_device,
    ),
    _Tool(
        schema={
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
        handler=_update_device,
    ),
    _Tool(
        schema={
            "name": "get_device",
            "description": (
                "Returns full detail for a single device: attributes, supported standards/protocols, and "
                "any attached documents (manuals, label photos, notes). Use this once you've identified "
                "which device is relevant (e.g. from list_devices) and need its complete detail — "
                "troubleshooting, reviewing existing documentation, or before editing it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
        handler=_get_device,
    ),
    _Tool(
        schema={
            "name": "get_compatible_devices",
            "description": (
                "Returns devices/standards compatible with the given device — use this for replacement or "
                "new-purchase recommendations, to check what would work with what's already at home."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
        handler=_get_compatible_devices,
    ),
    _Tool(
        schema={
            "name": "retire_device",
            "description": (
                "Retires (soft-deletes) a device that's been removed, replaced, or no longer exists at "
                "home. It stops appearing in list_devices but its history isn't erased. Only call this "
                "when the user clearly confirms the device is gone — ask first if it's ambiguous."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"device_id": {"type": "string"}},
                "required": ["device_id"],
            },
        },
        handler=_retire_device,
    ),
    _Tool(
        schema={
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
        handler=_attach_document,
    ),
    _Tool(
        schema={
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
        handler=_unreachable_extract_device_data,
    ),
    _Tool(
        schema={
            "name": "generate_document",
            "description": (
                "Renders a document you've already written and sends it back to the user as a file "
                "attachment on this channel. Use this whenever the user asks for a report, an export, or "
                "any other document in a specific file format (e.g. 'send me a PDF report of my devices', "
                "'give me a CSV of what's registered'). You write the full content yourself first (pulling "
                "whatever data you need from list_devices/get_device first) — this tool only converts your "
                "text into the requested file; it doesn't generate content on its own. This takes a moment; "
                "say a brief sentence before calling it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_type": {"type": "string", "enum": ["pdf", "csv", "txt", "markdown"]},
                    "filename": {"type": "string", "description": "Without extension — it's added automatically."},
                    "content": {
                        "type": "string",
                        "description": (
                            "The full document content, already written out. For pdf/txt/markdown, plain "
                            "text — lines starting with '#'/'##' are rendered as headings, everything else "
                            "as a normal paragraph (no bold/italic/tables support). For csv, comma-separated "
                            "rows including a header row."
                        ),
                    },
                },
                "required": ["file_type", "filename", "content"],
            },
        },
        handler=_unreachable_generate_document,
    ),
    _Tool(
        schema={
            "name": "generate_image",
            "description": (
                "Gets a picture and sends it back to the user as a photo on this channel. If device_id is "
                "given, FIRST checks whether a real photo is already attached to that device (from "
                "get_device's device_document list, kind 'photo') and reuses it — only if none exists does "
                "it try an actual web image search, falling back to generating one if that finds nothing. "
                "Use this whenever the user asks to see, find, or generate a picture/photo/image of "
                "something. Always pass device_id when the picture is of a device already in the "
                "inventory (call get_device/list_devices first if you don't have it) — this both enables "
                "the reuse check and gives search the best chance of finding the real thing. This takes a "
                "moment; say a brief sentence before calling it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "device_id": {
                        "type": "string",
                        "description": (
                            "The device this picture is of, if it's one already in the inventory — enables "
                            "reusing a photo already on file instead of searching/generating a new one. "
                            "Omit for anything that isn't an existing device."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "What the picture should show — be as specific as possible (exact brand/model "
                            "if it's a real product) so a real photo search has the best chance of finding it. "
                            "Used even when device_id is given, in case no existing photo is found."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": "Short, descriptive, without extension — it's added automatically and always .jpg.",
                    },
                },
                "required": ["query", "filename"],
            },
        },
        handler=_unreachable_generate_image,
    ),
]

TOOL_SCHEMAS: list[dict[str, Any]] = [t.schema for t in _TOOLS]
_HANDLERS: dict[str, Handler] = {t.name: t.handler for t in _TOOLS}

_unknown_pause_names = (ASYNC_TOOL_NAMES | CONFIRM_TOOL_NAMES) - _HANDLERS.keys()
assert not _unknown_pause_names, f"ASYNC_TOOL_NAMES/CONFIRM_TOOL_NAMES reference undeclared tools: {_unknown_pause_names}"


async def dispatch(pg: PostgrestClient, name: str, tool_input: dict[str, Any]) -> Any:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown or unsupported-here tool: {name!r}")
    return await handler(pg, tool_input)


def make_executor(pg: PostgrestClient) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """A tool executor closed over this turn's `pg` — factored out so
    `orchestrator/main.py` doesn't redefine the same closure at each of its
    call sites (the normal turn, the doc-ingestion resume, and the
    approval-confirmation resume)."""

    async def executor(name: str, tool_input: dict[str, Any]) -> Any:
        return await dispatch(pg, name, tool_input)

    return executor
