# image-generation-worker

Gets a real picture for the `generate_image` tool and hands it back to `orchestrator` to deliver as a channel attachment (a Telegram photo, not a generic file). Tries an actual web image search first — a real product photo for something like "mi lavadora Balay 3TS984B" beats an AI approximation — and only falls back to Gemini image generation when search finds nothing usable or isn't configured at all. Always normalizes the result to a real JPEG regardless of source format.

This service has **no MQTT, no PostgREST** — reached over `orchestrator`'s small internal HTTP API, same pattern as `doc-ingestion-worker`/`doc-generation-worker`. Unlike those two, it does keep its own Gemini client (for the generation fallback) *and* makes its own external HTTP calls (Google's Custom Search API + downloading the winning result) — the one service that needs both.

## How it works

- Exposes `POST /generate-image` (aiohttp, port `8080` — not exposed to the host, only reachable from `orchestrator` over the Docker network). `orchestrator` calls this when Gemini calls the `generate_image` tool (see `orchestrator/orchestrator/actions.py`) with a `query` (what the picture should show) and a suggested `filename`.
- The endpoint replies `202 Accepted` immediately (fire-and-forget) and does the actual work in a background task, same as the other two workers. Each request runs under a semaphore (`maxConcurrency` at a time).
- **`search.py`**: if `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_CX` are configured, queries Google's Custom Search JSON API with `searchType=image`, then downloads the first result that's actually reachable and looks like a real image (`image/jpeg`, `image/png`, or `image/webp`). Returns `None` (not an error) if either secret is missing, or if search runs but nothing usable comes back — either way the caller just falls through to generation.
- **`generate.py`**: a direct Gemini image-generation call (model name from appconfig's `imageModel`, not hardcoded — see the module's own docstring for why: the default hasn't been verified against the live API yet the way the text model's `gemini-flash-latest` was).
- **`convert.py`**: normalizes whatever came back (search result or Gemini output, in whatever format) into a real JPEG (`Pillow`) — flattens transparency onto plain RGB rather than letting a PNG-with-alpha fail to save as JPEG.
- Once done, calls back to `orchestrator`'s `POST /internal/image/result` with the result — base64-encoded JPEG bytes + a sanitized filename + which path actually produced it (`"search"`/`"generated"`) on success, or an error on failure — via `shared.internal_client.InternalApiClient` (bounded retry, not an infinite reconnect loop).
- `orchestrator` attaches the file to the reply as soon as the callback arrives, as an `AttachmentKind.IMAGE` — `telegram-adapter` sends `image` attachments with Telegram's `sendPhoto` (an inline preview), not `sendDocument` (a generic file), for the best delivery on that channel.

## Configuration

Like every service here, this reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why. Below are just the parts this service actually reads.

### Secrets (`barbarasecrets.env`)

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Used for the image-generation fallback (`generate.py`) — every request needs some way to produce an image even when search finds nothing |
| `ORCHESTRATOR_URL` | no (default `http://orchestrator:8080`) | Base URL of `orchestrator`'s internal API — the default is the fixed docker-compose service hostname/port, only override for a non-standard deployment |
| `GOOGLE_CSE_API_KEY` | no (default: search disabled) | API key for Google's Custom Search JSON API — see below for how to get one |
| `GOOGLE_CSE_CX` | no (default: search disabled) | The Search Engine ID (`cx`) of a Programmable Search Engine configured for image search — see below |

No `MQTT_*` or `POSTGREST_URL` needed — this service has no MQTT/Postgres connection of its own.

**Getting `GOOGLE_CSE_API_KEY`/`GOOGLE_CSE_CX`** (optional — without them, every request just uses Gemini generation directly):
1. Create an API key in [Google Cloud Console](https://console.cloud.google.com/apis/credentials) with the "Custom Search API" enabled.
2. Create a Programmable Search Engine at [programmablesearchengine.google.com](https://programmablesearchengine.google.com/) — enable "Image search" and "Search the entire web" in its settings, then copy its Search Engine ID (`cx`).
3. The free tier is limited (100 queries/day as of this writing) — fine for a single household, not for heavy use.

### AppConfig (`appconfigDev/appconfig.json`)

```json
{
  "image_generation_worker": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "port": 8080,
    "maxConcurrency": 2,
    "imageModel": "gemini-2.5-flash-image"
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8080` | Port for the internal HTTP API (`/generate-image`). Not exposed to the host. **Not hot-reloadable** — same reason as `web-adapter`'s `port` |
| `maxConcurrency` | `2` | Max number of images produced at the same time. Hot-reloadable |
| `imageModel` | `gemini-2.5-flash-image` | Gemini model used for the generation fallback — **verify this against the live API before relying on it** (see `generate.py`'s docstring); hot-reload-visible but only read per request, so an appconfig change takes effect on the very next generation |
