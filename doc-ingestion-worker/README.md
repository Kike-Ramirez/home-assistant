# doc-ingestion-worker

Turns a photo of a device's label or manual into structured data (brand, model, specs, category) using Claude vision. Runs as its own queue with bounded concurrency, specifically so a slow vision extraction never blocks the normal chat flow for anyone else.

## How it works

- Consumes `home/events/doc_ingestion` — requests published by `orchestrator` whenever a user sends a photo during the device-onboarding flow.
- Each request is processed under a semaphore (`maxConcurrency` concurrent extractions at a time); the rest queue up rather than piling on Claude's API all at once.
- The photo attachment can be either a public URL (Telegram's file URLs) or a `data:` base64 URI (from `web-adapter`, whose server usually isn't reachable outside the LAN) — this service detects which one it got and builds the right kind of image block for Claude either way.
- Extraction uses the same structured-output mechanism as `orchestrator` (`shared.claude.call_structured`): the result is forced into a Pydantic model (`DeviceExtraction`) and validated, with a retry if Claude's response doesn't match the expected shape — no unchecked `json.loads` on free-text output.
- Publishes the result (success + extracted data, or failure + error message) back to `home/events/doc_ingestion_result`, which `orchestrator` consumes to show the user a draft to confirm.
- Never writes to Postgres directly — this service only ever proposes a draft; `orchestrator` is the one that saves it once the user confirms.

## Configuration

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Your Claude API key — see the note below |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

No `POSTGREST_URL` needed — see above, this service never talks to Postgres.

### AppConfig (`appconfig.json`)

```json
{
  "doc_ingestion_worker": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "maxConcurrency": 2,
    "claudeModel": "claude-sonnet-5"
  }
}
```

| Key | Default | Description |
|---|---|---|
| `maxConcurrency` | `2` | Max number of photo extractions running at the same time |
| `claudeModel` | `claude-sonnet-5` | Which Claude model to use for vision extraction |

Both hot-reloadable — changing `maxConcurrency` live recreates the internal semaphore with the new limit (in-flight extractions on the old one aren't migrated, so there's a brief window where effective concurrency can exceed the new limit until those finish — fine for a home project, worth knowing about).

### Getting an Anthropic API key

Same key as `orchestrator` — see [its README](../orchestrator/README.md#getting-an-anthropic-api-key) for how to get one. You can reuse the same key across both services (or use separate ones if you want separate usage tracking in the Anthropic Console).
