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
import base64
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from shared.message import (
    Attachment,
    AttachmentKind,
    MessageType,
    NormalizedMessage,
    inbound_topic,
    outbound_topic,
)
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, mqtt_secrets, system, telegram_secrets
from .formatting import markdown_to_telegram_html

logger = logging.getLogger("telegram_adapter")

CHANNEL = "telegram"

# How long to wait, after the last item of a Telegram album ("media group")
# arrives, before treating it as complete and publishing it as one message
# with several attachments — Telegram delivers each photo/document of an
# album as its own separate Update, not as a single message.
ALBUM_DEBOUNCE_SECONDS = 1.5

# HTML as the default parse_mode: Gemini's replies use a light Markdown
# subset (see SYSTEM_PROMPT), converted to Telegram-safe HTML by
# markdown_to_telegram_html() before every send_message call — HTML needs far
# less escaping than Telegram's own MarkdownV2 for arbitrary model output.
bot = Bot(token=telegram_secrets.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Persistent MQTT connection for the life of the process, reused for both
# publishing (inbound) and consuming (outbound) — opening a new connection
# per message would be unnecessary and expensive under load.
_mqtt = ManagedMqttConnection("telegram_adapter")

# media_group_id -> messages collected so far, and the pending flush task —
# a message with several attachments (a Telegram album) arrives as several
# separate Updates sharing the same media_group_id.
_pending_albums: dict[str, list[Message]] = {}
_album_flush_tasks: dict[str, asyncio.Task] = {}

# user_id -> the "typing..." indicator currently shown for that chat, started
# as soon as we publish an inbound message and stopped as soon as any reply
# for that user comes back — gives the household visible feedback that the
# assistant is working on it, even before the model's own text arrives.
_typing_senders: dict[str, ChatActionSender] = {}

_http_client = httpx.AsyncClient()


async def _publish_inbound(msg: NormalizedMessage) -> None:
    await _mqtt.publish(inbound_topic(CHANNEL, msg.user_id), msg.model_dump_json())
    sender = ChatActionSender.typing(bot=bot, chat_id=int(msg.user_id))
    await sender.__aenter__()
    previous = _typing_senders.pop(msg.user_id, None)
    if previous is not None:
        await previous.__aexit__(None, None, None)
    _typing_senders[msg.user_id] = sender


async def _stop_typing(user_id: str) -> None:
    sender = _typing_senders.pop(user_id, None)
    if sender is not None:
        await sender.__aexit__(None, None, None)


async def _telegram_file_url(file_id: str) -> str:
    file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{telegram_secrets.bot_token}/{file.file_path}"


async def _attachment_from_message(message: Message) -> Attachment | None:
    if message.photo:
        file_url = await _telegram_file_url(message.photo[-1].file_id)
        return Attachment(kind=AttachmentKind.IMAGE, media_type="image/jpeg", url_or_data=file_url)
    if message.document:
        file_url = await _telegram_file_url(message.document.file_id)
        return Attachment(
            kind=AttachmentKind.DOCUMENT,
            media_type=message.document.mime_type,
            url_or_data=file_url,
            filename=message.document.file_name,
        )
    return None


async def _flush_album(media_group_id: str) -> None:
    await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
    messages = _pending_albums.pop(media_group_id, [])
    _album_flush_tasks.pop(media_group_id, None)
    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)
    attachments = [a for a in await asyncio.gather(*(_attachment_from_message(m) for m in messages)) if a is not None]
    caption = next((m.caption for m in messages if m.caption), None)
    first = messages[0]

    msg = NormalizedMessage(
        channel=CHANNEL,
        user_id=str(first.chat.id),
        conversation_id=str(first.chat.id),
        type=MessageType.TEXT,
        content=caption,
        attachments=attachments,
    )
    await _publish_inbound(msg)


@dp.message()
async def on_telegram_message(message: Message) -> None:
    user_id = str(message.chat.id)
    # MVP phase: one conversation per Telegram chat. Keeps things simple —
    # the orchestrator upserts home.conversation by (channel, channel_conversation_id).
    conversation_id = str(message.chat.id)

    if message.media_group_id and (message.photo or message.document):
        # Part of an album — buffer it and (re)schedule the flush instead of
        # publishing immediately, so every attachment in the album ends up on
        # the same NormalizedMessage (CLAUDE.md's multi-attachment handling).
        group_id = message.media_group_id
        _pending_albums.setdefault(group_id, []).append(message)
        existing_task = _album_flush_tasks.get(group_id)
        if existing_task is not None:
            existing_task.cancel()
        _album_flush_tasks[group_id] = asyncio.create_task(_flush_album(group_id))
        return

    if message.photo:
        attachment = await _attachment_from_message(message)
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=conversation_id,
            type=MessageType.TEXT,
            content=message.caption,
            attachments=[attachment] if attachment else [],
        )
    elif message.document:
        attachment = await _attachment_from_message(message)
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=conversation_id,
            type=MessageType.TEXT,
            content=message.caption,
            attachments=[attachment] if attachment else [],
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


@dp.callback_query()
async def on_telegram_callback(query: CallbackQuery) -> None:
    """A tap on an inline button sent alongside an approval prompt
    (`shared.message.Action` — see orchestrator's Human-in-the-Loop flow for
    `create_device`/`update_device`/`retire_device`). `query.data` is exactly
    the button's `value` ("approve"/"reject"), forwarded as-is as the content
    of a CALLBACK-typed inbound message."""
    await query.answer()  # stop Telegram's client-side loading spinner
    if query.message is None or query.data is None:
        return

    user_id = str(query.message.chat.id)
    msg = NormalizedMessage(
        channel=CHANNEL,
        user_id=user_id,
        conversation_id=str(query.message.chat.id),
        type=MessageType.CALLBACK,
        content=query.data,
    )
    await _publish_inbound(msg)
    # Remove the buttons so a second tap can't resolve an already-answered prompt.
    try:
        await bot.edit_message_reply_markup(chat_id=query.message.chat.id, message_id=query.message.message_id, reply_markup=None)
    except Exception:
        logger.exception("Couldn't clear the approval buttons after a tap")


def _build_reply_markup(msg: NormalizedMessage) -> InlineKeyboardMarkup | None:
    if not msg.actions:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=action.label, callback_data=action.value) for action in msg.actions]]
    )


async def _attachment_bytes(attachment: Attachment) -> bytes:
    if attachment.url_or_data.startswith("data:"):
        _, _, b64_data = attachment.url_or_data.partition(",")
        return base64.b64decode(b64_data)
    response = await _http_client.get(attachment.url_or_data)
    response.raise_for_status()
    return response.content


async def _handle_outbound_message(mqtt_message) -> None:
    msg = NormalizedMessage.model_validate_json(mqtt_message.payload)
    await _stop_typing(msg.user_id)

    chat_id = int(msg.user_id)
    reply_markup = _build_reply_markup(msg)

    if msg.content:
        await bot.send_message(chat_id=chat_id, text=markdown_to_telegram_html(msg.content), reply_markup=reply_markup)
    elif reply_markup is not None:
        # No text, but there are buttons (Aprobar/Rechazar) to show — Telegram
        # rejects an empty-text sendMessage outright, so this needs *some* text.
        await bot.send_message(chat_id=chat_id, text="…", reply_markup=reply_markup)
    elif not msg.attachments:
        # No text, no buttons, no attachments — nothing to actually send.
        # orchestrator is expected to never produce this (see DEFAULT_DONE_FALLBACK
        # in orchestrator/llm.py), but silently dropping here is still safer than
        # calling Telegram's API with an empty string, which it rejects outright.
        logger.warning("Outbound message for user %s has no content, actions, or attachments — dropping", msg.user_id)

    for attachment in msg.attachments:
        data = await _attachment_bytes(attachment)
        filename = attachment.filename or "documento"
        if attachment.kind == AttachmentKind.IMAGE:
            # sendPhoto renders inline (a real preview in the chat) instead of
            # a generic downloadable file — the better delivery for a photo,
            # whether it came from an image search or was generated (both
            # always JPEG — see image-generation-worker/convert.py).
            await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(data, filename=filename))
        else:
            await bot.send_document(chat_id=chat_id, document=BufferedInputFile(data, filename=filename))


async def main() -> None:
    _ready_logged = False

    async def on_mqtt_connect(client) -> None:
        nonlocal _ready_logged
        if not _ready_logged:
            _ready_logged = True
            logger.info("telegram-adapter ready: connected to Telegram (long polling) and MQTT.")
        await _mqtt.consume(client, outbound_topic(CHANNEL, "+"), _handle_outbound_message)

    # Telegram polling and the MQTT connection have independent lifecycles:
    # if MQTT drops, it reconnects on its own (with backoff) without
    # affecting Telegram message reception.
    mqtt_task = asyncio.create_task(maintain_mqtt_connection(mqtt_secrets, system, on_mqtt_connect))
    config_task = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))
    try:
        await dp.start_polling(bot)
    finally:
        mqtt_task.cancel()
        config_task.cancel()
        await _http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
