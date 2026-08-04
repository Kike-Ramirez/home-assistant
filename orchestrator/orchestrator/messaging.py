"""Helpers for replying to the user by publishing on the channel's outbound topic."""

from __future__ import annotations

from shared.message import Action, Attachment, MessageType, NormalizedMessage, outbound_topic
from shared.mqtt_client import ManagedMqttConnection


async def reply(
    client: ManagedMqttConnection,
    incoming: NormalizedMessage,
    text: str,
    actions: list[Action] | None = None,
    attachments: list[Attachment] | None = None,
) -> None:
    await reply_raw(client, incoming.channel, incoming.user_id, incoming.conversation_id, text, actions, attachments)


async def reply_raw(
    client: ManagedMqttConnection,
    channel: str,
    user_id: str,
    conversation_id: str,
    text: str,
    actions: list[Action] | None = None,
    attachments: list[Attachment] | None = None,
) -> None:
    outgoing = NormalizedMessage(
        channel=channel,
        user_id=user_id,
        conversation_id=conversation_id,
        type=MessageType.TEXT,
        content=text,
        actions=actions,
        attachments=attachments or [],
    )
    await client.publish(outbound_topic(channel, user_id), outgoing.model_dump_json(), qos=1)
