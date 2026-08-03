"""telegram-adapter: translates between Telegram <-> the normalized message contract (MQTT).

Doesn't know about intents or conversation state — it just translates and
forwards (CLAUDE.md section 2: "the orchestrator never knows which channel a
message came from").

Long polling (`dp.start_polling`), no webhook: the process only ever opens
outbound connections (to Telegram and to the MQTT broker), same as every
other service here — zero ports to expose, zero TLS certificate to manage
just to receive Telegram updates.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from shared.message import (
    MessageType,
    NormalizedMessage,
    inbound_topic,
    outbound_topic,
)
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, mqtt_secrets, system, telegram_secrets

logger = logging.getLogger("telegram_adapter")

CHANNEL = "telegram"

bot = Bot(token=telegram_secrets.bot_token)
dp = Dispatcher()

# Persistent MQTT connection for the life of the process, reused for both
# publishing (inbound) and consuming (outbound) — opening a new connection
# per message would be unnecessary and expensive under load.
_mqtt = ManagedMqttConnection("telegram_adapter")


async def _publish_inbound(msg: NormalizedMessage) -> None:
    await _mqtt.publish(inbound_topic(CHANNEL, msg.user_id), msg.model_dump_json())


@dp.message()
async def on_telegram_message(message: Message) -> None:
    user_id = str(message.chat.id)
    # MVP phase: one conversation per Telegram chat. Keeps things simple —
    # the orchestrator upserts home.conversation by (channel, channel_conversation_id).
    conversation_id = str(message.chat.id)

    if message.photo:
        file = await bot.get_file(message.photo[-1].file_id)
        file_url = f"https://api.telegram.org/file/bot{telegram_secrets.bot_token}/{file.file_path}"
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=conversation_id,
            type=MessageType.PHOTO,
            content=message.caption,
            attachments=[file_url],
        )
    elif message.text and message.text.startswith("/"):
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=conversation_id,
            type=MessageType.COMMAND,
            content=message.text,
        )
    else:
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=conversation_id,
            type=MessageType.TEXT,
            content=message.text or "",
        )

    await _publish_inbound(msg)


async def _handle_outbound_message(mqtt_message) -> None:
    msg = NormalizedMessage.model_validate_json(mqtt_message.payload)
    await bot.send_message(chat_id=int(msg.user_id), text=msg.content or "")


async def main() -> None:
    logger.info("telegram-adapter starting up (long polling)")
    # Telegram polling and the MQTT connection have independent lifecycles:
    # if MQTT drops, it reconnects on its own (with backoff) without
    # affecting Telegram message reception.
    mqtt_task = asyncio.create_task(
        maintain_mqtt_connection(
            mqtt_secrets,
            system,
            lambda client: _mqtt.consume(client, outbound_topic(CHANNEL, "+"), _handle_outbound_message),
        )
    )
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))
    try:
        await dp.start_polling(bot)
    finally:
        mqtt_task.cancel()
        config_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
