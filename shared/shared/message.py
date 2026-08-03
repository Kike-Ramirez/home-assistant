"""Normalized MQTT message contract (CLAUDE.md section 5) and topic naming."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    COMMAND = "command"
    TELEMETRY = "telemetry"  # reserved — Barbara Standard Data Model, not used in the home MVP


class NormalizedMessage(BaseModel):
    channel: str
    user_id: str
    conversation_id: str
    type: MessageType
    content: str | None = None
    attachments: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def inbound_topic(channel: str, user_id: str) -> str:
    return f"home/inbound/{channel}/{user_id}"


def outbound_topic(channel: str, user_id: str) -> str:
    return f"home/outbound/{channel}/{user_id}"


# Internal event topics between orchestrator <-> doc-ingestion-worker.
DOC_INGESTION_REQUEST_TOPIC = "home/events/doc_ingestion"
DOC_INGESTION_RESULT_TOPIC = "home/events/doc_ingestion_result"

REMINDER_EVENT_TOPIC = "home/events/reminder"
PRICE_ALERT_EVENT_TOPIC = "home/events/price_alert"
FIRMWARE_UPDATE_EVENT_TOPIC = "home/events/firmware_update"


class DocIngestionRequest(BaseModel):
    """Published by orchestrator when the user sends a photo (flow 1)."""

    conversation_id: str  # home.conversation id (uuid), for internal correlation
    channel_conversation_id: str  # conversation_id from the MQTT contract (channel's chat/thread)
    channel: str
    user_id: str
    attachment_url: str


class DocIngestionResult(BaseModel):
    """Published by doc-ingestion-worker with the extracted data (draft, not yet saved)."""

    conversation_id: str  # home.conversation id (uuid)
    channel_conversation_id: str
    channel: str
    user_id: str
    success: bool
    error: str | None = None
    draft_device: dict | None = None  # display_name, brand, model, device_type_code, attributes, isa95_area
