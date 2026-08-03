"""Helpers for replying to the user by publishing on the channel's outbound topic."""

from __future__ import annotations

import aiomqtt

from shared.message import MessageType, NormalizedMessage, outbound_topic


async def reply(client: aiomqtt.Client, incoming: NormalizedMessage, text: str) -> None:
    await reply_raw(client, incoming.channel, incoming.user_id, incoming.conversation_id, text)


async def reply_raw(client: aiomqtt.Client, channel: str, user_id: str, conversation_id: str, text: str) -> None:
    outgoing = NormalizedMessage(
        channel=channel,
        user_id=user_id,
        conversation_id=conversation_id,
        type=MessageType.TEXT,
        content=text,
    )
    await client.publish(outbound_topic(channel, user_id), payload=outgoing.model_dump_json(), qos=1)
