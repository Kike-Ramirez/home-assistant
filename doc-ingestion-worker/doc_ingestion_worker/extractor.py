"""Extracts structured data from a photo of a device's label/manual, via the
active LLM engine's vision + structured-output support (`shared.engines`).

The attachment can be a public URL (telegram-adapter, which exposes
`api.telegram.org/file/...`) or a base64 data URI (web-adapter, whose server
is usually only reachable on the LAN and therefore isn't a URL most engines
could fetch themselves) — the message contract (CLAUDE.md section 5)
accounted for this from the start, and `Engine.call_structured` handles
either transport internally.

Uses `Engine.call_structured` (tool_choice/JSON-mode + Pydantic + retry,
depending on the engine) instead of asking for free-text JSON and trusting
`json.loads` with no schema validation at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from shared.engines import get_engine
from shared.message import Attachment, AttachmentKind

from .config import SERVICE_NAME, appconfig, system

_DEFAULT_MODELS = {"gemini": "gemini-flash-latest", "anthropic": "claude-sonnet-5"}
_ENGINE_NAME = appconfig.get("engine", "gemini")

engine = get_engine(
    _ENGINE_NAME,
    SERVICE_NAME,
    appconfig.get("model", _DEFAULT_MODELS.get(_ENGINE_NAME, "gemini-flash-latest")),
    system.connect_timeout_seconds,
)

EXTRACTION_SYSTEM_PROMPT = (
    "Analyze the photo of a home appliance/device's label or manual that the "
    "user sent and extract its data. If you can't read a field with "
    "confidence, leave it empty. `device_type_code` must be snake_case and "
    "stable (same device type -> same code)."
)


class DeviceExtraction(BaseModel):
    device_type_code: str
    device_type_name: str
    display_name: str  # short name to identify it by, e.g. brand + model
    brand: str | None = None
    model: str | None = None
    isa95_area: str | None = None  # room, if identifiable from the photo/context
    attributes: dict[str, Any] = Field(default_factory=dict)


async def extract_device_data(attachment: str) -> dict[str, Any]:
    result = await engine.call_structured(
        EXTRACTION_SYSTEM_PROMPT,
        "Extract this device's data.",
        [Attachment(kind=AttachmentKind.IMAGE, url_or_data=attachment)],
        "extract_device",
        DeviceExtraction,
    )
    return result.model_dump()
