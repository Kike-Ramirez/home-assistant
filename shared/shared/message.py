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


# Note: doc-ingestion-worker <-> orchestrator and notifier-scheduler ->
# orchestrator used to be MQTT event topics here. They're now direct HTTP
# calls to orchestrator's internal API instead (see shared/internal_client.py
# and each service's README) — orchestrator is the only service left with an
# MQTT connection besides the two channel adapters, so the bus only carries
# home/inbound/* and home/outbound/* now.


class DocIngestionRequest(BaseModel):
    """Body of orchestrator's POST to doc-ingestion-worker's `/extract` (flow 1)."""

    conversation_id: str  # home.conversation id (uuid), for internal correlation
    channel_conversation_id: str  # conversation_id from the message contract (channel's chat/thread)
    channel: str
    user_id: str
    attachment_url: str


class DocIngestionResult(BaseModel):
    """Body of doc-ingestion-worker's POST back to orchestrator's
    `/internal/doc-ingestion/result`, with the extracted data (draft, not yet saved)."""

    conversation_id: str  # home.conversation id (uuid)
    channel_conversation_id: str
    channel: str
    user_id: str
    success: bool
    error: str | None = None
    draft_device: dict | None = None  # display_name, brand, model, device_type_code, attributes, isa95_area
