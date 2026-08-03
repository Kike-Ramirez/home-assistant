"""Normalized MQTT message contract (CLAUDE.md section 5) and topic naming."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    TEXT = "text"
    COMMAND = "command"
    CALLBACK = "callback"  # a button press on a previously sent `actions` prompt (see Action below)
    TELEMETRY = "telemetry"  # reserved — Barbara Standard Data Model, not used in the home MVP


class AttachmentKind(str, Enum):
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"  # modeled for future use — no adapter captures it and Claude's Messages API
    # has no audio content-block type, so nothing consumes it yet (CLAUDE.md section 10).


class Attachment(BaseModel):
    kind: AttachmentKind
    media_type: str | None = None  # MIME type, e.g. "image/jpeg", "application/pdf"
    url_or_data: str  # public URL or a data: base64 URI, same convention as before
    filename: str | None = None


class Action(BaseModel):
    """One button on an outbound message — used for Human-in-the-Loop
    approval prompts (CLAUDE.md section 6, flow 1's write-tool gate). `value`
    is what comes back verbatim as the `content` of the resulting inbound
    CALLBACK message — orchestrator only ever sends "approve"/"reject" today,
    kept generic here so a future prompt with more than two options doesn't
    need a schema change."""

    label: str
    value: str


class NormalizedMessage(BaseModel):
    channel: str
    user_id: str
    conversation_id: str
    type: MessageType
    content: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    actions: list[Action] | None = None  # outbound only — renders as inline buttons where the channel supports it
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
