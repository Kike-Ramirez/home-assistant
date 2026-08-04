# doc-generation-worker

Renders a document into actual file bytes (PDF/CSV/TXT/Markdown) and hands it back to `orchestrator` to deliver as a channel attachment. Mirrors `doc-ingestion-worker`'s shape in the opposite direction: that service turns a document into data (vision extraction), this one turns data — text Gemini already wrote — into a document. Runs as its own queue with bounded concurrency, so rendering a report never blocks the normal chat flow for anyone else.

This service has **no MQTT, no PostgREST, and no LLM connection of its own** — it does no content generation, only rendering, so unlike `doc-ingestion-worker` it doesn't need the one exception that service has for `GeminiClient`. It's reached over `orchestrator`'s small internal HTTP API, same pattern as `doc-ingestion-worker`.

## How it works

- Exposes `POST /generate` (aiohttp, port `8080` — not exposed to the host, only reachable from `orchestrator` over the Docker network). `orchestrator` calls this when Gemini calls the `generate_document` tool (see `orchestrator/orchestrator/actions.py`) — Gemini has already written the document's full content itself (pulling whatever it needs from `list_devices`/`get_device` first); this service only converts that text into the requested file format.
- The endpoint replies `202 Accepted` immediately (fire-and-forget) and renders in a background task, same as `doc-ingestion-worker`'s `/extract`.
- Each request is processed under a semaphore (`maxConcurrency` at a time).
- `file_type` is one of `pdf`, `csv`, `txt`, `markdown` (`doc_generation_worker/renderer.py`):
  - `csv`/`txt`/`markdown` are just UTF-8-encoded as-is — the model already wrote the exact text/rows.
  - `pdf` uses `fpdf2` (pure Python, no system dependencies like Pango/Cairo) with a minimal layout: lines starting with `#`/`##` render as headings, everything else as a wrapped paragraph. No tables/bold/italic — deliberately simple for a first version.
- Once rendering finishes, calls back to `orchestrator`'s `POST /internal/doc-generation/result` with the result — base64-encoded bytes + filename + media type on success, or an error message on failure — via `shared.internal_client.InternalApiClient` (bounded retry, not an infinite reconnect loop).
- `orchestrator` attaches the file to the reply on whichever channel the request came from (`shared.message.Attachment`) as soon as the callback arrives — it doesn't wait for the rest of the conversational turn to finish.

## Configuration

Like every service here, this reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why. Below are just the parts this service actually reads.

### Secrets (`barbarasecrets.env`)

| Variable | Required | Description |
|---|---|---|
| `ORCHESTRATOR_URL` | yes | Base URL of `orchestrator`'s internal API, e.g. `http://orchestrator:8080` |

No `MQTT_*`, `POSTGREST_URL`, or `GEMINI_API_KEY` needed — this service has no MQTT/Postgres/LLM connection of its own.

### AppConfig (`appconfigDev/appconfig.json`)

```json
{
  "doc_generation_worker": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "port": 8080,
    "maxConcurrency": 2
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8080` | Port for the internal HTTP API (`/generate`). Not exposed to the host — reachable only from `orchestrator` over the Docker network. **Not hot-reloadable** — same reason as `web-adapter`'s `port` |
| `maxConcurrency` | `2` | Max number of documents rendered at the same time. Hot-reloadable — recreates the internal semaphore with the new limit live |
