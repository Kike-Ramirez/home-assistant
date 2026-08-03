# doc-ingestion-worker

Turns a photo of a device's label or manual into structured data (brand, model, specs, category) using Gemini vision. Runs as its own queue with bounded concurrency, specifically so a slow vision extraction never blocks the normal chat flow for anyone else.

This service has **no MQTT and no PostgREST connection** — under the "`orchestrator` owns every external/shared connection" design (see the root [README's design notes](../README.md#design-notes)), it's reached over a small internal HTTP API instead. The one deliberate exception is Gemini itself: this service keeps its own `GeminiClient` purely for vision extraction, since routing that specific call through `orchestrator` would mean a circular hop (`orchestrator` → this service → back to `orchestrator` for Gemini → back to this service) for no benefit.

## How it works

- Exposes `POST /extract` (aiohttp, port `8080` — not exposed to the host, only reachable from `orchestrator` over the Docker network). `orchestrator` calls this when — and only when — Gemini itself decides a photo is a device label/manual worth onboarding, by calling the `extract_device_data` tool (see `orchestrator/orchestrator/actions.py`). A photo isn't automatically routed here just for being a photo — Gemini might instead treat it as an error screenshot to troubleshoot, or documentation to attach to an existing device.
- The endpoint replies `202 Accepted` immediately (fire-and-forget) and processes the extraction in a background task — a vision call can take a while, and this keeps `orchestrator` from blocking on it.
- Each request is processed under a semaphore (`maxConcurrency` concurrent extractions at a time); the rest queue up rather than piling on Gemini's API all at once.
- The photo attachment can be either a public URL (Telegram's file URLs) or a `data:` base64 URI (from `web-adapter`, whose server usually isn't reachable outside the LAN) — this service detects which one it got and builds the right kind of `inline_data` part for Gemini either way.
- Extraction uses `GeminiClient.call_structured` (`shared/shared/gemini_client.py`): the result is forced into a Pydantic model (`DeviceExtraction`) via JSON-schema-constrained output and validated, with a retry if Gemini's response doesn't match the expected shape — no unchecked `json.loads` on free-text output.
- Once extraction finishes, calls back to `orchestrator`'s `POST /internal/doc-ingestion/result` with the result (success + extracted data, or failure + error message), which `orchestrator` uses to show the user a draft to confirm. This callback goes through `shared.internal_client.InternalApiClient` — a thin `httpx` wrapper with a couple of bounded retries, not an infinite reconnect loop, since it's a one-off call tied to a request that's already in flight.
- Never writes to Postgres directly — this service only ever proposes a draft; `orchestrator` is the one that saves it, and only after the user approves the resulting `create_device` call (Human-in-the-Loop — see `orchestrator/README.md`).

## Configuration

Like every service here, this reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why. Below are just the parts this service actually reads.

### Secrets (`barbarasecrets.env`)

Fill in these variables in the repo-root [`barbarasecrets.env`](../barbarasecrets.env):

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Your Google Gemini API key — see the note below |
| `ORCHESTRATOR_URL` | yes | Base URL of `orchestrator`'s internal API, e.g. `http://orchestrator:8080` |

No `MQTT_*` or `POSTGREST_URL` needed — see above, this service has no MQTT or Postgres connection of its own.

### AppConfig (`appconfigDev/appconfig.json`)

`doc-ingestion-worker`'s own slice of the repo-root [`appconfigDev/appconfig.json`](../appconfigDev/appconfig.json):

```json
{
  "doc_ingestion_worker": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "port": 8080,
    "maxConcurrency": 2,
    "model": "gemini-flash-latest",
    "temperature": 0.1
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8080` | Port for the internal HTTP API (`/extract`). Not exposed to the host — reachable only from `orchestrator` over the Docker network. **Not hot-reloadable** — same reason as `web-adapter`'s `port` |
| `maxConcurrency` | `2` | Max number of photo extractions running at the same time. Hot-reloadable — changing it live recreates the internal semaphore with the new limit (in-flight extractions on the old one aren't migrated, so there's a brief window where effective concurrency can exceed the new limit until those finish — fine for a home project, worth knowing about) |
| `model` | `gemini-flash-latest` | Which Gemini model to use for vision extraction. Only read once, at process start |
| `temperature` | `0.1` | Sampling temperature — kept low here since extraction should be as deterministic as possible. Only read once, at process start |

### Getting a Gemini API key

Same key as `orchestrator` — see [its README](../orchestrator/README.md#getting-a-gemini-api-key) for how to get one. You can reuse the same key across both services (or use separate ones if you want separate usage tracking).
