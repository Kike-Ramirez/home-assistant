# orchestrator

The brain. Every inbound message from every channel ends up here; Gemini decides what to do with it via tool calls, and this service just executes them (or, for a write, gates it on human approval first) — there's no flow dispatch or intent branching of its own. It's **stateless** on purpose — all conversation state (the Gemini message transcript) lives in Postgres (via PostgREST), not in process memory, so it could scale to multiple replicas behind the same MQTT topics without any of them stepping on each other.

It's also the **only service connected to MQTT, PostgREST, and the Gemini API** (aside from `telegram-adapter`/`web-adapter`, which own their own MQTT connections as channel adapters, and `doc-ingestion-worker`'s narrow Gemini-vision exception — see the root [README's design notes](../README.md#design-notes)). Neither `doc-ingestion-worker` nor `doc-generation-worker` has a direct connection to any of those systems; both reach `orchestrator` through the small internal HTTP API described below instead.

## How it works

Consumes `home/inbound/+/+` (any channel, any user) over MQTT. For each inbound message, `main.py::handle_inbound`:

1. Builds this turn's Gemini content (text + any image/document attachments) and appends it to `conversation.state.history`.
2. Runs `shared.gemini_client.GeminiClient.run_agent_loop` with the full tool list from `actions.py` and the system prompt from `llm.py`. Gemini decides everything from here — which tool(s) to call, or none, and what to say.
3. Persists the updated history and sends whatever Gemini's final text was back to the channel.

Three things can pause a turn instead of running inline:

- **`extract_device_data`** (vision extraction via `doc-ingestion-worker`) needs an HTTP round trip that can't be awaited inside the loop — calling it pauses the turn (`conversation.state.pending_agent_turn`) until the result comes back over the internal API below.
- **`generate_document`** (file rendering via `doc-generation-worker`) pauses the same way — Gemini has already written the document's content, this just waits for it to be rendered into bytes. As soon as the callback arrives, the file is delivered to the user as an attachment on that same reply, whether or not the turn goes on to do something else.
- **`create_device` / `update_device` / `retire_device`** (writes to the inventory) pause the same way, but instead of async work, `orchestrator` asks the user to approve/reject via the channel — a Telegram message with Aprobar/Rechazar buttons, or a plain "sí"/"no" text reply on channels without real buttons (see `security_guard.py`). The action only actually reaches PostgREST once approved.

Only one of these can be pending per conversation at a time — a second inbound message while one is pending gets a "still waiting" reply instead of starting a new turn. A single inbound message can still carry several attachments (e.g. a Telegram album `telegram-adapter` buffers into one message) — `conversation.state.pending_attachments` keeps them available, and `_finish_paused_turn` re-enters the same kickoff logic on every resume, so the model works through them one at a time (extract one, approve/reject the resulting write, extract the next, ...) instead of the turn giving up after the first.

It also runs a small internal HTTP server (aiohttp, port `8080` — not exposed to the host, only reachable over the Docker network):

| Endpoint | Called by | What it does |
|---|---|---|
| `POST /internal/doc-ingestion/result` | `doc-ingestion-worker` | Delivers the result of a `/extract` request once extraction finishes (success + draft device data, or failure + error) — resumes the paused agent-loop turn via `resume_agent_loop` |
| `POST /internal/doc-generation/result` | `doc-generation-worker` | Delivers the result of a `/generate` request once rendering finishes (success + base64 file bytes, or failure + error) — resumes the paused turn and attaches the file to the reply |

None of this uses keyword matching or a fixed intent enum. **Every** decision — what to do with a message, whether a photo is a new device or something else, whether the user confirmed a draft or corrected it — is Gemini choosing (or not choosing) a tool, driven by the full conversation it can see. `doc-ingestion-worker`'s vision extraction still validates its output with `GeminiClient.call_structured()` (JSON-schema-constrained output, validated against a Pydantic model, retries on mismatch), but that's the one place structured extraction still matters; nothing in `orchestrator` classifies intent anymore.

### The modules

| Module | Role |
|---|---|
| `llm.py` | Instantiates the one `GeminiClient` this service uses, and holds `SYSTEM_PROMPT` — all the conversational behavior lives here, as a prompt, not as code branches |
| `actions.py` | The tool schemas Gemini sees, and the plain function dispatcher that executes them against PostgREST (`registry.py`) — no judgment, it runs whatever it's told |
| `security_guard.py` | The Human-in-the-Loop gate: builds the approval prompt/buttons, parses a yes/no reply, and is the one place a write tool actually reaches `actions.dispatch()` |
| `registry.py` | PostgREST read/write surface for `home.device`/`device_document` — what `actions.py`'s handlers call into |
| `conversation.py` | `home.conversation` CRUD (get-or-create by channel, state patch) |
| `messaging.py` | Publishes replies to `home/outbound/<channel>/<user_id>` |

### The tools (`actions.py`)

| Tool | Approval | What it does |
|---|---|---|
| `list_devices` | auto | Returns the household's inventory (optionally with each device's supported standards/protocols) |
| `get_device` | auto | Full detail for one device: attributes, standards, attached documents |
| `get_compatible_devices` | auto | Devices/standards compatible with a given one (`home.compatible_devices` RPC) — replacement/purchase recommendations |
| `attach_document` | auto | Attaches a manual/photo/note to an existing device (`home.device_document`) — purely additive, nothing destroyed |
| `create_device` | **Human-in-the-Loop** | Saves a new device — typically after `extract_device_data` + the user confirming a draft, or from a plain description |
| `update_device` | **Human-in-the-Loop** | Edits an existing device — corrections, added detail |
| `retire_device` | **Human-in-the-Loop** | Soft-deletes a device (`status='retired'`) — drops out of `list_devices`, history kept |
| `extract_device_data` | n/a (async) | Vision-extracts a photo already in the conversation as a device label/manual — has real async work behind it, see above. For a multi-attachment message, called once per attachment across several pause/resume round trips, never batched |
| `generate_document` | n/a (async) | Renders content Gemini already wrote into a PDF/CSV/TXT/Markdown file and sends it back as an attachment — the other tool with real async work behind it |
| `web_search` | auto (togglable) | Gemini's built-in `google_search` grounding tool — current info the model doesn't already know |

Troubleshooting answers, course lessons+quizzes, and replacement recommendations are **not** tools — they're just Gemini's own final text, using `list_devices`/`get_device`/`get_compatible_devices`/`web_search` as needed. Quiz grading works the same way: Gemini re-reads its own prior turns from the conversation history rather than a tracked question index.

## Configuration

Like every service here, this one reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why there's one `appconfig.json` and one secrets file for the whole app instead of one per service. Below are just the parts of those shared files that `orchestrator` actually reads.

### Secrets (`barbarasecrets.env`)

Fill in these variables in the repo-root [`barbarasecrets.env`](../barbarasecrets.env):

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | yes | Your Google Gemini API key — see below |
| `POSTGREST_URL` | yes | Base URL of the PostgREST instance, e.g. `http://postgrest:3000` |
| `DOC_INGESTION_WORKER_URL` | no (default `http://doc-ingestion-worker:8080`) | Base URL of `doc-ingestion-worker`'s internal API — the default is the fixed docker-compose service hostname/port, only override for a non-standard deployment |
| `DOC_GENERATION_WORKER_URL` | no (default `http://doc-generation-worker:8080`) | Base URL of `doc-generation-worker`'s internal API — the default is the fixed docker-compose service hostname/port, only override for a non-standard deployment |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |
| `TELEGRAM_ADMIN_CHAT_ID` | no (default: skipped) | Your Telegram chat id — if set, orchestrator sends a friendly one-time "I'm up" welcome message here right after startup |

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
    "model": "gemini-flash-latest",
    "temperature": 0.2,
    "maxTokens": 4096,
    "webSearchEnabled": true
  }
}
```

| Key | Default | Description |
|---|---|---|
| `port` | `8080` | Port for the internal HTTP API (`/internal/doc-ingestion/result`, `/internal/doc-generation/result`). Not exposed to the host — reachable only from `doc-ingestion-worker`/`doc-generation-worker` over the Docker network. **Not hot-reloadable** — same reason as `web-adapter`'s `port` (the server's already bound the socket) |
| `model` | `gemini-flash-latest` | Which Gemini model to call for everything in this service — a Google-maintained alias that always points at their current flash-tier model, avoiding a hardcoded dated model name being deprecated out from under this default. Only read once, at process start (needs a restart to change) |
| `temperature` | `0.2` | Sampling temperature for the agent loop — lower favors consistent, predictable tool-calling behavior over creative variance. Only read once, at process start |
| `maxTokens` | `4096` | Max output tokens per agent-loop turn |
| `webSearchEnabled` | `true` | Whether to give the model its web-search tool (read live each turn — no restart needed to flip it) |

Everything except `model`/`temperature` (only read at construction) hot-reloads — see [`shared/README.md`](../shared/README.md).

### Getting a Gemini API key

[Google AI Studio](https://aistudio.google.com/apikey) → create an API key → that's your `GEMINI_API_KEY`. Keep an eye on usage/billing — this service calls Gemini on essentially every user message.

- **Hitting quota/rate-limit errors (429)?** New keys default to Google AI Studio's free tier, which has low per-minute/per-day limits. Enable Cloud Billing for the project behind your key (AI Studio → API Keys or Projects page → "Set up Billing", minimum $10 in prepaid credits) to move to the pay-as-you-go tier and raise those limits — see [ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits) and the [billing page](https://ai.google.dev/gemini-api/docs/billing). A quota error no longer crashes the conversation either way (see `shared.gemini_client`'s `api_error_fallback` handling), but it does mean Gemini can't respond until the quota resets or billing is enabled.
- **Treat it like any other credential**: never commit it (it only ever belongs in `barbarasecrets.env`, gitignored), and check usage regularly in AI Studio → Dashboard → Usage — that's also where you'd notice a leaked key being used unexpectedly.
