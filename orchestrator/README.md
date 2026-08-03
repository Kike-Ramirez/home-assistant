# orchestrator

The brain. Every inbound message from every channel ends up here; Claude decides what to do with it via tool calls, and this service just executes them — there's no flow dispatch or intent branching of its own. It's **stateless** on purpose — all conversation state (the Claude message transcript) lives in Postgres (via PostgREST), not in process memory, so it could scale to multiple replicas behind the same MQTT topics without any of them stepping on each other.

It's also the **only service connected to MQTT, PostgREST, and the Claude API** (aside from `telegram-adapter`/`web-adapter`, which own their own MQTT connections as channel adapters, and `doc-ingestion-worker`'s narrow Claude-vision exception — see the root [README's design notes](../README.md#design-notes)). `doc-ingestion-worker` and `notifier-scheduler` have no direct connection to any of those systems; they reach `orchestrator` through the small internal HTTP API described below instead.

## How it works

Consumes `home/inbound/+/+` (any channel, any user) over MQTT. For each inbound message, `main.py::handle_inbound`:

1. Builds this turn's Claude content blocks (text + any image/document attachments — see `shared.claude.build_content_blocks`) and appends them to `conversation.state.history`.
2. Runs `shared.claude.run_agent_loop` with the full tool list from `tools.py` and the system prompt from `claude_client.py`. Claude decides everything from here — which tool(s) to call, or none, and what to say.
3. Persists the updated history and sends whatever Claude's final text was back to the channel.

The one tool that isn't executed inline is `extract_device_data` (vision extraction via `doc-ingestion-worker`) — it needs an HTTP round trip that can't be awaited inside the loop, so calling it pauses the whole turn (`conversation.state.pending_agent_turn`) until the result comes back over the internal API below.

It also runs a small internal HTTP server (aiohttp, port `8080` — not exposed to the host, only reachable over the Docker network) for the two services that used to talk over MQTT/PostgREST directly:

| Endpoint | Called by | What it does |
|---|---|---|
| `POST /internal/doc-ingestion/result` | `doc-ingestion-worker` | Delivers the result of a `/extract` request once extraction finishes (success + draft device data, or failure + error) — resumes the paused agent-loop turn via `resume_agent_loop` |
| `POST /internal/reminders/check` | `notifier-scheduler` | Triggers one full reminder-check cycle: reads due reminders from PostgREST, sends the ones that are ready, words the ones that need Claude's help via `word_reminder()`, and reschedules/marks-sent as appropriate. Returns `{"processed": <count>}` |

None of this uses keyword matching or a fixed intent enum. **Every** decision — what to do with a message, whether a photo is a new device or something else, whether the user confirmed a draft or corrected it — is Claude choosing (or not choosing) a tool, driven by the full conversation it can see. `doc-ingestion-worker`'s vision extraction still validates its output with `shared.claude.call_structured()` (forces a tool call via `tool_choice`, validates against a Pydantic model, retries on mismatch — same mechanism a library like `instructor` would give you, without the extra dependency weight), but that's the one place structured extraction still matters; nothing in `orchestrator` classifies intent anymore.

### The tools (`tools.py`)

| Tool | What it does |
|---|---|
| `list_devices` | Returns the household's inventory (optionally with each device's supported standards/protocols) — Claude calls this itself when it needs the data, instead of it being force-fed into every prompt |
| `create_device` | Saves a new device — typically after `extract_device_data` + the user confirming a draft, or from a plain description |
| `update_device` | Edits an existing device — corrections, added detail |
| `attach_document` | Attaches a manual/photo/note to an existing device (`home.device_document`) |
| `extract_device_data` | Vision-extracts a photo already in the conversation as a device label/manual — the one asynchronous tool, see above |
| `schedule_reminder` | Creates a `home.reminder` row for anything the user asks to be reminded about |
| `web_search` / `web_fetch` | Anthropic's built-in server tools — current info, and fetching a URL the user shared |

Troubleshooting answers, course lessons+quizzes, and replacement recommendations are **not** tools — they're just Claude's own final text, using `list_devices`/`web_search`/`web_fetch` as needed. Quiz grading works the same way: Claude re-reads its own prior turns from the conversation history rather than a tracked question index.

## Configuration

Like every service here, this one reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why there's one `appconfig.json` and one secrets file for the whole app instead of one per service. Below are just the parts of those shared files that `orchestrator` actually reads.

### Secrets (`barbarasecrets.env`)

Fill in these variables in the repo-root [`barbarasecrets.env`](../barbarasecrets.env):

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes (if `engine` is `gemini`, the default) | Your Google Gemini API key — see below |
| `ANTHROPIC_API_KEY` | yes (if `engine` is `anthropic`) | Your Claude API key — Anthropic is still a selectable engine, just not the default anymore |
| `POSTGREST_URL` | yes | Base URL of the PostgREST instance, e.g. `http://postgrest:3000` |
| `DOC_INGESTION_WORKER_URL` | yes | Base URL of `doc-ingestion-worker`'s internal API, e.g. `http://doc-ingestion-worker:8080` |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

If any required variable is missing, the service logs an error naming it exactly and keeps retrying instead of crashing.

**About PostgREST itself** (not this service's config, but relevant): it needs to be configured with `PGRST_DB_SCHEMA=home` and `PGRST_DB_ANON_ROLE=app_service`. There's no JWT and no per-request role here — see the comment at the top of [`db/schema.sql`](../db/schema.sql) for why that's the right call for a single-household, single-trusted-user project (and what to add if that ever changes).

### AppConfig (`appconfigDev/appconfig.json`)

`orchestrator`'s own slice of the repo-root [`appconfigDev/appconfig.json`](../appconfigDev/appconfig.json):

```json
{
  "orchestrator": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "port": 8080,
    "engine": "gemini",
    "model": "gemini-flash-latest",
    "maxTokens": 4096,
    "webSearchEnabled": true
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8080` | Port for the internal HTTP API (`/internal/doc-ingestion/result`, `/internal/reminders/check`). Not exposed to the host — reachable only from `doc-ingestion-worker`/`notifier-scheduler` over the Docker network. **Not hot-reloadable** — same reason as `web-adapter`'s `port` (the server's already bound the socket) |
| `engine` | `gemini` | Which LLM engine to use — `gemini` or `anthropic` (see [`shared/shared/engines/`](../shared/shared/engines)). Only read once, at process start (needs a restart to switch) |
| `model` | `gemini-flash-latest` (or `claude-sonnet-5` for the `anthropic` engine) | Which model that engine calls for everything in this service — `gemini-flash-latest` is a Google-maintained alias that always points at their current flash-tier model, avoiding hardcoded model names being deprecated out from under this default |
| `maxTokens` | `4096` | Max output tokens per agent-loop turn |
| `webSearchEnabled` | `true` | Whether to give the model its web-search tool(s) (read live each turn — no restart needed to flip it) |

Everything except `engine`/`model` (which are only read at construction) hot-reloads — see [`shared/README.md`](../shared/README.md).

### Getting an API key

- **Gemini (default)**: [Google AI Studio](https://aistudio.google.com/apikey) → create an API key → that's your `GEMINI_API_KEY`.
- **Anthropic** (if you switch `engine` to `anthropic`): [Anthropic Console](https://console.anthropic.com/) → **API Keys** → create a key → that's your `ANTHROPIC_API_KEY`.

Keep an eye on usage/billing for whichever provider is active — this service calls it on essentially every user message.

> The Gemini engine hasn't been exercised against the live API yet (built from the `google-genai` package's type definitions + mocked-response tests) — see CLAUDE.md section 10. `web_search`/`web_fetch` on the Anthropic engine use the `_20260209` dynamic-filtering tool variants, confirmed current as of the session that added them.
