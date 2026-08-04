"""web-adapter: minimal web chat channel — no Telegram, no external dependencies.

Built as the "always available" channel: doesn't depend on setting up a bot
or on anyone in the house having Telegram, and it's the handiest one for
debugging from day one (just open a browser against the node). Same
normalized message contract as every other adapter (CLAUDE.md section 5) —
the orchestrator doesn't know or care which channel a message came from.

No authentication: built for a trusted home LAN, same simplicity trade-off
as the rest of the project (see the note about PostgREST without JWT in
db/schema.sql). If this node ever became reachable from outside the LAN,
add at least a shared password before exposing it like this.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from shared.message import Attachment, AttachmentKind, MessageType, NormalizedMessage, inbound_topic, outbound_topic
from shared.mqtt_client import ManagedMqttConnection, maintain_mqtt_connection
from shared.settings import watch_appconfig

from .config import SERVICE_NAME, appconfig, mqtt_secrets, system

logger = logging.getLogger("web_adapter")

CHANNEL = "web"
STATIC_DIR = Path(__file__).parent / "static"

# Persistent MQTT connection for the whole process (same approach as the
# rest of the services here — see telegram-adapter).
_mqtt = ManagedMqttConnection("web_adapter")

# user_id -> active WebSocketResponse. No queue/persistence across
# reconnects: if the browser is closed, any outbound messages from that
# moment are dropped — acceptable for a debugging/fallback channel, not for
# the main one (that's Telegram, delivered through the app itself).
_connections: dict[str, web.WebSocketResponse] = {}


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def _publish_inbound(msg: NormalizedMessage) -> None:
    await _mqtt.publish(inbound_topic(CHANNEL, msg.user_id), msg.model_dump_json())


async def _handle_client_payload(user_id: str, data: dict) -> None:
    if data.get("type") == "attachment":
        media_type = data.get("media_type") or ""
        kind = AttachmentKind.IMAGE if media_type.startswith("image/") else AttachmentKind.DOCUMENT
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=user_id,  # one conversation per browser session
            type=MessageType.TEXT,
            content=data.get("caption"),
            attachments=[
                Attachment(kind=kind, media_type=media_type or None, url_or_data=data["data_uri"], filename=data.get("filename"))
            ],
        )
    else:
        msg = NormalizedMessage(
            channel=CHANNEL,
            user_id=user_id,
            conversation_id=user_id,
            type=MessageType.TEXT,
            content=data.get("text", ""),
        )
    await _publish_inbound(msg)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    user_id = request.query.get("user_id")
    if not user_id:
        raise web.HTTPBadRequest(text="Missing user_id")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _connections[user_id] = ws
    logger.info("Web connection opened: %s", user_id)

    try:
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            try:
                await _handle_client_payload(user_id, json.loads(message.data))
            except Exception:
                logger.exception("Error processing inbound message from %s", user_id)
    finally:
        _connections.pop(user_id, None)
        logger.info("Web connection closed: %s", user_id)

    return ws


def _render_text(msg: NormalizedMessage) -> str:
    """web-adapter is the secondary/debug channel — no real button UI, so an
    approval prompt (`msg.actions`, e.g. Aprobar/Rechazar) just gets a plain
    text hint appended. The user replies with a normal text message ("sí"/
    "no", among others orchestrator recognizes — see `_parse_confirmation_text`)."""
    text = msg.content or ""
    if msg.actions:
        options = " / ".join(action.label for action in msg.actions)
        text = f"{text}\n\n({options} — responde con sí o no)"
    return text


async def _handle_outbound_message(mqtt_message) -> None:
    msg = NormalizedMessage.model_validate_json(mqtt_message.payload)
    ws = _connections.get(msg.user_id)
    if ws is not None and not ws.closed:
        attachments = [
            {"filename": a.filename, "media_type": a.media_type, "data_uri": a.url_or_data} for a in msg.attachments
        ]
        await ws.send_str(json.dumps({"text": _render_text(msg), "attachments": attachments}))


async def on_startup(app: web.Application) -> None:
    logger.info("web-adapter starting up on port %s", appconfig.get("port", 8090))
    # The HTTP/WebSocket server and the MQTT connection have independent
    # lifecycles: if MQTT drops, it reconnects on its own (with backoff)
    # without affecting browser connections that are already open.
    app["mqtt_task"] = asyncio.create_task(
        maintain_mqtt_connection(
            mqtt_secrets,
            system,
            lambda client: _mqtt.consume(client, outbound_topic(CHANNEL, "+"), _handle_outbound_message),
        )
    )
    # NOTE: 'port' is the one real exception to "appconfig always hot-reloads"
    # — aiohttp has already bound the socket by the time it starts, so
    # changing the port live has no effect without restarting the process
    # (there's no way to re-bind an HTTP server that's already listening).
    app["config_task"] = asyncio.create_task(watch_appconfig(SERVICE_NAME, system, appconfig))


async def on_shutdown(app: web.Application) -> None:
    app["mqtt_task"].cancel()
    app["config_task"].cancel()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket_handler)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=appconfig.get("port", 8090))
