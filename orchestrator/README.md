# orchestrator

The brain. Every inbound message from every channel ends up here; this service figures out what the user wants and runs the right flow. It's **stateless** on purpose — all conversation state lives in Postgres (via PostgREST), not in process memory, so it could scale to multiple replicas behind the same MQTT topics without any of them stepping on each other.

## How it works

Consumes `home/inbound/+/+` (any channel, any user) and `home/events/doc_ingestion_result` (results coming back from `doc-ingestion-worker`). For each inbound message:

1. **Photo attached?** → kicks off the device-onboarding flow (asks `doc-ingestion-worker` to extract the data).
2. **Conversation mid-flow** (`awaiting_confirmation`, `awaiting_extraction`, `course_quiz`)? → routes to whatever that flow needs next.
3. **Otherwise** → asks Claude to classify the intent (troubleshooting / course / replacement / other) and dispatches accordingly.

None of this uses keyword matching. Every single "what did the user mean" decision — intent classification, yes/no confirmation, which quiz option they picked — goes through `shared.claude.call_structured()`: Claude is forced (via `tool_choice`) to return an object matching a Pydantic model, which gets validated, with a retry if it doesn't match. It's the same mechanism a library like `instructor` would give you, without the extra dependency weight (see the note in `claude_client.py` for why `instructor` specifically got skipped).

### The 4 flows

| Flow | Module | What happens |
|---|---|---|
| Add a device | `flows/onboarding.py` | Photo → asks `doc-ingestion-worker` to extract data → shows a draft → confirms with the user → saves via PostgREST |
| Troubleshooting | `flows/troubleshooting.py` | Passes Claude the **full** device inventory (no pre-filtering) plus the question; Claude decides which device it's about and whether it needs `web_search` |
| Quick course + quiz | `flows/course.py` | Claude generates a short lesson and a multiple-choice quiz; each answer (however the user phrases it — a letter, a number, free text) is interpreted by Claude, not parsed |
| Replacement / new purchase | `flows/replacement.py` | Passes Claude the inventory plus which standards/protocols each device supports (Zigbee, Matter, ...) and lets it use `web_search` for pricing/popularity |

## Configuration

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Your Claude API key — see below |
| `POSTGREST_URL` | yes | Base URL of the PostgREST instance, e.g. `http://postgrest:3000` |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

If any required variable is missing, the service logs an error naming it exactly and keeps retrying instead of crashing.

**About PostgREST itself** (not this service's config, but relevant): it needs to be configured with `PGRST_DB_SCHEMA=home` and `PGRST_DB_ANON_ROLE=app_service`. There's no JWT and no per-request role here — see the comment at the top of [`db/schema.sql`](../db/schema.sql) for why that's the right call for a single-household, single-trusted-user project (and what to add if that ever changes).

### AppConfig (`appconfig.json`)

```json
{
  "orchestrator": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "claudeModel": "claude-sonnet-5",
    "maxTokens": 4096,
    "webSearchEnabled": true
  }
}
```

| Key | Default | Description |
|---|---|---|
| `claudeModel` | `claude-sonnet-5` | Which Claude model to call for everything in this service |
| `maxTokens` | `4096` | Max output tokens for the free-text answers (troubleshooting, replacement recommendations) |
| `webSearchEnabled` | `true` | Whether to give Claude the `web_search` tool for troubleshooting/replacement answers |

All hot-reloadable — see [`shared/README.md`](../shared/README.md).

### Getting an Anthropic API key

1. Sign up / log in at the [Anthropic Console](https://console.anthropic.com/).
2. Go to **API Keys** and create a new key.
3. That's your `ANTHROPIC_API_KEY`. Keep an eye on usage/billing in the console — this service calls Claude on essentially every user message.

> **Heads up before deploying:** the `web_search` tool identifier used here (`web_search_20250305` in `claude_client.py`) may change over time — double-check it against the current [`anthropic` SDK docs](https://docs.anthropic.com/) before going live.
