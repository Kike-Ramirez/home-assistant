"""Extracts structured data from a photo of a device's label/manual, via
Gemini's vision + structured-output support (`shared.gemini_client`).

The attachment can be a public URL (telegram-adapter, which exposes
`api.telegram.org/file/...`) or a base64 data URI (web-adapter, whose server
is usually only reachable on the LAN and therefore isn't a URL Gemini could
fetch itself) — the message contract (CLAUDE.md section 5) accounted for
this from the start, and `GeminiClient.call_structured` handles either
transport internally.

Uses `GeminiClient.call_structured` (JSON-mode + Pydantic + retry) instead of
asking for free-text JSON and trusting `json.loads` with no schema validation
at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from shared.gemini_client import GeminiClient
from shared.message import Attachment, AttachmentKind
from shared.settings import GeminiSecrets, load_secrets

from .config import SERVICE_NAME, appconfig, system

_secrets = load_secrets(GeminiSecrets, SERVICE_NAME, system.connect_timeout_seconds)
client = GeminiClient(
    _secrets.api_key,
    appconfig.get("model", "gemini-flash-latest"),
    temperature=appconfig.get("temperature", 0.1),  # low — extraction should be as deterministic as possible
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
    result = await client.call_structured(
        EXTRACTION_SYSTEM_PROMPT,
        "Extract this device's data.",
        [Attachment(kind=AttachmentKind.IMAGE, url_or_data=attachment)],
        "extract_device",
        DeviceExtraction,
    )
    return result.model_dump()
