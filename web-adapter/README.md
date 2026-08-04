# web-adapter

A minimal web chat, built to be the **always-available fallback channel**: it needs no external account, no bot setup, nothing — just a browser pointed at the node's IP. If someone in the house doesn't have Telegram (or doesn't want it), this is how they talk to the assistant. It's also the easiest channel to use for local debugging, since there's nothing external to configure.

Same [normalized message contract](../shared/README.md) as every other channel adapter (`channel="web"`) — `orchestrator` doesn't know or care that this isn't Telegram.

## How it works

- Serves a single self-contained HTML page (`static/index.html` — no build step, no JS framework) at `/`, and a WebSocket endpoint at `/ws?user_id=<uuid>`.
- The browser generates a random `user_id` on first load (`crypto.randomUUID()`) and keeps it in `localStorage` — that's the whole "session" model. **There's no login.** This is a deliberate simplicity choice for a trusted home LAN — see the note in the code and in the root README's design notes. If this node ever became reachable from outside your LAN, you'd want to add at least a shared password before exposing it like this.
- Text messages and file uploads (any type — image, PDF, whatever; read client-side as a base64 data URI via `FileReader`) both go out over the same WebSocket as a small JSON payload, get turned into a `NormalizedMessage` with an `Attachment` (kind `image` or `document`, decided from the file's MIME type), and published to `home/inbound/web/<user_id>`.
- Messages on `home/outbound/web/<user_id>` get pushed back down the matching WebSocket connection, if one's open — as `{"text": ..., "attachments": [...]}`. Any `attachments` (e.g. a file from the `generate_document` tool) are passed straight through as `data:` URIs the page renders as a plain download link (`<a href download>`), no server-side decoding needed here. The page also shows a local "Escribiendo..." hint from the moment it sends a message until the next reply arrives — its equivalent of Telegram's native typing indicator, entirely client-side.
- The HTTP/WebSocket server and the MQTT connection run as independent background tasks — same as `telegram-adapter` — so a dropped MQTT connection doesn't kill open browser sessions.
- No message queue: if nobody's got the page open when a reply comes back, it's dropped. That's an acceptable trade for a fallback/debugging channel — it's not the primary way anyone's expected to use this day-to-day.

### Why attachments here work differently from Telegram

Gemini has no `url` source type at all — every attachment gets downloaded and sent as inline bytes, whether it started as a public `api.telegram.org` URL or a base64 `data:` URI from here. So in practice `telegram-adapter` and `web-adapter` attachments already end up handled the same way by `shared.gemini_client`; the only difference is where the bytes come from (an HTTP fetch vs. decoding what the browser already sent).

### No real approval buttons — a text fallback instead

This is the secondary/debug channel, so it doesn't render Telegram-style inline buttons. When `orchestrator`'s Human-in-the-Loop gate needs an approve/reject decision, the outbound message's `actions` (e.g. "✅ Aprobar" / "❌ Rechazar") are appended to the message text as a hint instead — you just reply with a plain "sí"/"no" (or "aprobar"/"rechazar"), which `orchestrator` parses the same way it would a Telegram user typing instead of tapping a button.

## Configuration

Like every service here, this reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why. Below are just the parts this service actually reads.

### Secrets (`barbarasecrets.env`)

Fill in these variables in the repo-root [`barbarasecrets.env`](../barbarasecrets.env):

| Variable | Required | Description |
|---|---|---|
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

No external API credentials needed — that's the point of this being the fallback channel.

### AppConfig (`appconfigDev/appconfig.json`)

`web-adapter`'s own slice of the repo-root [`appconfigDev/appconfig.json`](../appconfigDev/appconfig.json):

```json
{
  "web_adapter": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "port": 8090
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8090` | HTTP/WebSocket listen port. **Not hot-reloadable** — the server has already bound the socket by the time a config reload would notice a change, so this one needs a restart (see [`docker-compose.yml`](../docker-compose.yml), which maps `8090:8090` to match). |

## Trying it out

Once the service is running, open `http://<host>:8090` in a browser. Type a message, hit send, or attach a photo/document — what happens next (onboarding, troubleshooting, attaching it to an existing device) is Gemini's call, not this adapter's.
