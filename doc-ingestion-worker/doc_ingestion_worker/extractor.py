"""Extracts structured data from a photo of a device's label/manual via Claude vision.

The attachment can be a public URL (telegram-adapter, which exposes
`api.telegram.org/file/...`) or a base64 data URI (web-adapter, whose server
is usually only reachable on the LAN and therefore isn't a URL Claude could
fetch) — the message contract (CLAUDE.md section 5) accounted for
`"attachments": ["url_or_base64"]` from the start.

Uses `shared.claude.call_structured` (same mechanism as `orchestrator`:
tool_choice + Pydantic + retry) instead of asking Claude for free-text JSON
and trusting `json.loads` with no schema validation at all.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field
from shared.claude import call_structured

from .config import anthropic_secrets, appconfig

_client = AsyncAnthropic(api_key=anthropic_secrets.api_key)

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


def _image_block(attachment: str) -> dict[str, Any]:
    if attachment.startswith("data:"):
        header, _, data = attachment.partition(",")
        media_type = header.removeprefix("data:").split(";")[0] or "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": attachment}}


async def extract_device_data(attachment: str) -> dict[str, Any]:
    result = await call_structured(
        _client,
        appconfig.get("claudeModel", "claude-sonnet-5"),
        system=EXTRACTION_SYSTEM_PROMPT,
        user_content=[_image_block(attachment), {"type": "text", "text": "Extract this device's data."}],
        tool_name="extract_device",
        model=DeviceExtraction,
    )
    return result.model_dump()
