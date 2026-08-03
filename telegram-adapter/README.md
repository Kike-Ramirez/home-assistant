# telegram-adapter

Translates between Telegram and the [normalized message contract](../shared/README.md) — nothing more. It doesn't know about intents, conversation state, or flows; that's all `orchestrator`'s job. This adapter's only responsibility is: turn a Telegram update into a `NormalizedMessage` and publish it, and turn an outbound `NormalizedMessage` back into a Telegram reply.

Built with [`aiogram`](https://docs.aiogram.dev/) v3, using **long polling** (not a webhook): the process only ever opens outbound connections, to Telegram's API and to the MQTT broker. That means no port to expose, no public URL, no TLS certificate to manage — the container can sit entirely behind a home LAN with no inbound access at all.

## How it works

- On startup, it starts polling Telegram for updates and, separately, connects to MQTT and subscribes to `home/outbound/telegram/+`.
- Each incoming Telegram message (text, photo, or a `/command`) becomes a `NormalizedMessage` published to `home/inbound/telegram/<chat_id>`. Photos are sent along as their Telegram file URL (`api.telegram.org/file/...`) — that URL is public enough for Claude's vision API to fetch it directly, so no download/re-upload happens here.
- Each `NormalizedMessage` arriving on `home/outbound/telegram/<chat_id>` gets sent back to that chat as a plain text message.
- Telegram polling and the MQTT connection run as independent background tasks: if MQTT drops, it reconnects on its own (with backoff) without interrupting your ability to send messages — they just won't reach the orchestrator until the connection's back.

## Configuration

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Your bot's token — see below for how to get one |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

If any required variable is missing, the service logs an error naming it exactly and keeps retrying — it won't crash-loop the container.

### AppConfig (`appconfig.json`)

```json
{
  "telegram_adapter": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    }
  }
}
```

No service-specific parameters beyond `system` — see [`shared/README.md`](../shared/README.md) for what `debugLevel`/`connectTimeoutMs` do and how hot-reload works.

### Getting a Telegram bot token

1. Open a chat with [**@BotFather**](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a username ending in `bot`).
3. BotFather replies with a token that looks like `123456789:AAExampleTokenAbcDefGhiJklMnoPqrStuVwx` — that's your `TELEGRAM_BOT_TOKEN`.
4. Message your new bot once (anything) so it shows up as an active chat — Telegram won't deliver updates for a bot nobody has ever messaged.

That's it — no webhook URL, no domain, no certificate. Long polling means it works from day one on a bot running behind a home router.
