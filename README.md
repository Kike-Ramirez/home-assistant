# 🏠 Home Assistant on Barbara

A conversational home assistant that lives on a [Barbara](https://barbara.tech) edge node. You talk to it over Telegram (or a plain web chat), and it helps you catalog your home devices, troubleshoot them, learn about stuff, and figure out what to buy next.

This started as a personal project, but it's built on Barbara — an industrial edge computing platform — on purpose: the same patterns here (event bus, JSONB-first schema, ISA95-style location hierarchy) are meant to scale later into an industrial knowledge-base use case (PLCs, SCADA, Historians). None of that industrial stuff is built yet — it's just why some design choices look a little more "enterprise" than a home project strictly needs.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Docker Compose](https://img.shields.io/badge/deploy-docker--compose-2496ED)

---

## What it does

Talk to it like you'd talk to a knowledgeable housemate — send it text, a photo, a document, or a URL, in any combination, and it figures out what you mean:

- **Add a device** — snap a photo of a label or manual, it extracts brand/model/specs via Gemini vision, shows you a draft, you confirm (or correct it inline), and approve the save.
- **Edit, retire, or document a device** — correct a detail on something already registered, mark something as gone, or attach a manual/photo/note to it — Gemini tells the cases apart from context, not from a menu.
- **Troubleshoot** — ask about anything you've added ("why won't the washing machine drain?"), or just send a photo of the error code on the screen — it uses the device's saved specs plus a web search if needed, and walks you through a fix step by step.
- **Learn something** — ask for a quick course on any topic, get a short lesson plus a quiz to check you actually absorbed it.
- **Shop smart** — ask what to buy next (or what to replace something with), and it recommends options that are compatible with what you already have, with real prices/popularity pulled from the web.

No canned menus, no keyword commands, no fixed "intent" categories. Gemini decides everything itself by calling tools — orchestrator only executes read-only calls automatically. **Anything that writes, edits, or retires a device is held for your explicit approval first** (a Human-in-the-Loop gate — Aprobar/Rechazar buttons on Telegram, a plain sí/no reply anywhere else) before it actually reaches the database. See [Design notes](#design-notes) below.

## Architecture

```mermaid
flowchart LR
    subgraph Channels
        TG[Telegram]
        WEB[Web chat]
    end

    TG --> TA[telegram-adapter]
    WEB --> WA[web-adapter]

    TA -- MQTT inbound/outbound --> ORCH[orchestrator]
    WA -- MQTT inbound/outbound --> ORCH

    ORCH -- "POST /extract" --> DIW[doc-ingestion-worker]
    DIW -- "POST /internal/doc-ingestion/result" --> ORCH
    DIW -- vision --> GEMINI[Gemini API]

    ORCH -- "POST /generate" --> DGW[doc-generation-worker]
    DGW -- "POST /internal/doc-generation/result" --> ORCH

    ORCH -- "POST /generate-image" --> IGW[image-generation-worker]
    IGW -- "POST /internal/image/result" --> ORCH
    IGW -- "image search" --> CSE[Google Custom Search]
    IGW -- "generation fallback" --> GEMINI

    ORCH <-- REST --> PGRST[(PostgREST)]
    PGRST <--> PG[(Postgres)]
    ORCH -- "tool calls + approval gate" --> GEMINI

    MQTT{{MQTT broker<br/>Mosquitto}}
    TA -.-> MQTT
    WA -.-> MQTT
    ORCH -.-> MQTT
```

`orchestrator` is the only service with connections to MQTT, PostgREST, and (for the conversational flow) the Gemini API — it's the single owner of every external/shared connection except two named exceptions below. Everything else routes through it over a small internal HTTP API instead of touching those systems directly:

- **Channel adapters** (`telegram-adapter`, `web-adapter`) are the one deliberate exception on the inbound/outbound side: each manages its own Telegram/WebSocket connection *and* its own MQTT connection, publishing/subscribing directly to `home/inbound/<channel>/*` and `home/outbound/<channel>/*`. They're dumb translators — they turn whatever the channel gives them into a normalized message and publish it, and turn outbound normalized messages (including approval-prompt buttons and generated-file attachments) back into channel-native replies. The `orchestrator` never knows or cares which channel a message came from.
- **`doc-ingestion-worker`** is the other deliberate exception: it keeps its own Gemini client for vision extraction (see [Design notes](#design-notes) for why), but has no MQTT or PostgREST connection at all — `orchestrator` calls its `POST /extract` directly, and it calls back to `orchestrator`'s `POST /internal/doc-ingestion/result` when done, since a vision extraction can take a while and shouldn't block the caller.
- **`doc-generation-worker`** follows the same "reached over the internal API, no MQTT/PostgREST" shape as `doc-ingestion-worker`, but with no exception at all — it does no content generation of its own (Gemini already wrote the document's text before calling the `generate_document` tool), only renders it into a file, so it doesn't need a Gemini client either.
- **`image-generation-worker`** also has no MQTT/PostgREST, but it's the one service that needs both its own Gemini client *and* an external HTTP call of its own (Google's Custom Search API) — it tries a real photo search first for the `generate_image` tool, and only falls back to generating one with Gemini when search finds nothing usable or isn't configured.

Adding a new channel (WhatsApp, Slack, voice, whatever) is still just a matter of writing one more adapter that speaks the same MQTT contract — that part of the design is unchanged.

## Services

| Service | What it does | Notes |
|---|---|---|
| [`telegram-adapter`](./telegram-adapter) | Telegram ↔ normalized message, including approval buttons (inline keyboard) ↔ CALLBACK events | `aiogram` v3, long polling — no port exposed, no webhook/TLS to manage. Owns its own MQTT connection (exception #1) |
| [`web-adapter`](./web-adapter) | Minimal web chat ↔ normalized message | Always-on fallback channel — no Telegram account needed. The only service with an exposed port (8090). No real button UI — an approval prompt is rendered as text, answered with a plain sí/no reply. Owns its own MQTT connection (exception #2) |
| [`orchestrator`](./orchestrator) | The brain: runs Gemini's tool-use agent loop on every message, gating write tools on human approval — no intent routing of its own | Stateless — conversation state (the Gemini message transcript) lives in Postgres, not in the process, so it can scale to replicas. The only service connected to MQTT/PostgREST/Gemini for everything that isn't the one exception above; exposes a small internal HTTP API for `doc-ingestion-worker` |
| [`doc-ingestion-worker`](./doc-ingestion-worker) | Extracts device data from a photo via Gemini vision, when Gemini calls the `extract_device_data` tool | No MQTT, no PostgREST — called via `POST /extract`, calls back via `POST /internal/doc-ingestion/result`. Own bounded-concurrency queue so a slow extraction never blocks the chat flow. Keeps its own Gemini client (the one narrow exception to full centralization — see Design notes) |
| [`doc-generation-worker`](./doc-generation-worker) | Renders a document (PDF/CSV/TXT/Markdown) Gemini already wrote the content for, when Gemini calls the `generate_document` tool | No MQTT, no PostgREST, no Gemini client — called via `POST /generate`, calls back via `POST /internal/doc-generation/result` with the rendered file, which `orchestrator` delivers as a channel attachment |
| [`image-generation-worker`](./image-generation-worker) | Gets a real picture (Google Custom Search) or generates one (Gemini fallback) when Gemini calls the `generate_image` tool | No MQTT, no PostgREST — called via `POST /generate-image`, calls back via `POST /internal/image/result` with a JPEG, delivered as a Telegram photo (`sendPhoto`, not a generic file) |
| [`shared`](./shared) | Common library: message contract, MQTT/PostgREST clients, internal HTTP client, config/logging, the Gemini client | Not a deployable service — a workspace package the others depend on |

## External services (not part of this repo)

Reused from Barbara's Marketplace instead of reimplemented — see the [Design notes](#design-notes):

| Service | Used for | Where it comes from |
|---|---|---|
| **Mosquitto** (MQTT broker) | The event bus every service talks over | Barbara Marketplace connector |
| **PostgreSQL** | All persistent data (devices, conversations) | Barbara Marketplace connector |
| **PostgREST** | Turns the Postgres schema into a REST API — no custom CRUD service | Barbara Marketplace connector |
| **Google Gemini API** | Everything: the tool-use agent loop, vision extraction, troubleshooting/course/replacement content, `google_search` grounding | External API — you need your own key |
| **Telegram Bot API** | The Telegram channel | External API — you need your own bot token |

## Repo layout

Monorepo, `uv` workspace. One Python package per service, plus `shared`:

```
.
├── shared/                 # common library (all services depend on this)
├── telegram-adapter/
├── web-adapter/
├── orchestrator/
├── doc-ingestion-worker/
├── doc-generation-worker/
├── image-generation-worker/
├── db/schema.sql           # Postgres DDL (schema `home`, served by PostgREST)
├── appconfigDev/           # local-only: appconfig.json (app-level) + global.json (device-level, unused today)
├── barbarasecrets.env      # local-only: every service's secrets, in one file
├── docker-compose.yml      # for the Barbara edge node — pulls pre-built images from Docker Hub, no build/secrets/appconfig here
├── docker-compose-local.yml # for local debugging — builds from source, mounting appconfigDev/ and barbarasecrets.env
├── Dockerfile.service      # one generic Dockerfile for all 6 services, parameterized via build args
├── build-and-push-images.sh # builds + tags + pushes all 6 images to Docker Hub — see below
└── pyproject.toml          # workspace root
```

Each service has its own `README.md` (linked in the table above) with its specific config, required secrets, and how to get credentials for whatever external API it needs.

## Configuration: one shared appconfig + one shared secrets file

This project follows Barbara's own conventions exactly — see the [`boilerplate_01_python`](https://github.com/Barbaraedge/training_barbara_apps_development/tree/main/boilerplate_01_python) reference project — rather than inventing a per-service scheme:

- **`appconfigDev/appconfig.json`** — ONE JSON file for the whole app, one top-level key per service (`orchestrator`, `telegram_adapter`, ...), mounted read-only at `/appconfig/appconfig.json` in every container. This is Barbara's "Appconfig Application Level".
- **`appconfigDev/global.json`** — Barbara's "Appconfig Device Level", mounted at `/appconfig/global.json`. Not consumed by any service yet (nothing here needs device-wide config today), included so the file exists and there's a `load_global_config()` helper in `shared/shared/settings.py` ready for whenever something does.
- **`barbarasecrets.env`** — ONE env file with every service's secrets, injected identically into every container via `env_file:` in `docker-compose-local.yml`. This matches how Barbara Secrets actually work on a real node: one store per app/project, not one per docker-compose service — a container just reads whichever variables it needs and ignores the rest.

On a real Barbara node (`docker-compose.yml`), the platform injects both of these the same way, at the same paths — nothing in this project's code needs to know or care whether it's running locally or deployed.

## Running it locally

You don't need a Barbara node to try this out — `docker-compose-local.yml` runs everything standalone. You still need your **own** MQTT broker, Postgres+PostgREST, and API keys, since those aren't bundled (see the table above for exact requirements).

1. **Get the external pieces running.** Simplest path for local testing:
   - An MQTT broker reachable from your machine (e.g. `docker run -p 1883:1883 eclipse-mosquitto`).
   - A Postgres instance with `db/schema.sql` applied, and a PostgREST instance pointing at it (`PGRST_DB_SCHEMA=home`, `PGRST_DB_ANON_ROLE=app_service` — see [`db/schema.sql`](./db/schema.sql) for why there's no JWT here).
   - A Telegram bot token from [@BotFather](https://t.me/BotFather) (only needed if you want to test the Telegram channel — `web-adapter` needs nothing external).
   - A Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

2. **Fill in `barbarasecrets.env`** (repo root) with your real credentials — it ships with placeholders for every service's secrets in one file (see each service's README for which variables it actually reads). Never commit it with real values filled in.

3. **Build and run:**
   ```bash
   docker compose -f docker-compose-local.yml up --build
   ```

4. **Talk to it:**
   - Web chat: open `http://localhost:8090` in a browser.
   - Telegram: message your bot directly (it uses long polling, so no public URL or tunnel needed).

5. **Tweak config without rebuilding**: `appconfigDev/appconfig.json` (mounted read-only at `/appconfig/appconfig.json`) is watched and reloaded live — change any service's `debugLevel`, timeouts, or other settings and it picks it up within ~10s, no restart. `barbarasecrets.env` is the one thing that *does* need a restart to take effect (normal env var behavior).

If you're developing without Docker at all: it's a standard `uv` workspace, so `uv sync` at the repo root sets up every package, and `uv run python -m <package>.main` runs any one service directly (point `GEMINI_API_KEY`/`MQTT_*`/whatever it needs at real values via env vars, and `/appconfig/appconfig.json` at a local copy of `appconfigDev/appconfig.json` if you want non-default settings).

## Deploying to a Barbara node

`docker-compose.yml` (the one Barbara actually uses) doesn't build anything — it pulls pre-built images from Docker Hub, one repository per service:

| Service | Docker Hub repository | URL |
|---|---|---|
| `telegram-adapter` | `kikeramirez/home-assistant-telegram-adapter` | https://hub.docker.com/r/kikeramirez/home-assistant-telegram-adapter |
| `web-adapter` | `kikeramirez/home-assistant-web-adapter` | https://hub.docker.com/r/kikeramirez/home-assistant-web-adapter |
| `orchestrator` | `kikeramirez/home-assistant-orchestrator` | https://hub.docker.com/r/kikeramirez/home-assistant-orchestrator |
| `doc-ingestion-worker` | `kikeramirez/home-assistant-doc-ingestion-worker` | https://hub.docker.com/r/kikeramirez/home-assistant-doc-ingestion-worker |
| `doc-generation-worker` | `kikeramirez/home-assistant-doc-generation-worker` | https://hub.docker.com/r/kikeramirez/home-assistant-doc-generation-worker |
| `image-generation-worker` | `kikeramirez/home-assistant-image-generation-worker` | https://hub.docker.com/r/kikeramirez/home-assistant-image-generation-worker |

`docker-compose-local.yml` is unaffected by any of this — it still builds straight from `Dockerfile.service` for local debugging, always with your latest local changes, never from Docker Hub.

### Publishing images to Docker Hub

`build-and-push-images.sh` (repo root) builds from `Dockerfile.service` — same one `docker-compose-local.yml` uses — and pushes to the 6 repositories above. Log in once per machine/session (always interactive, never scripted):

```bash
docker login
```

**All 6 at once** (the common case — e.g. after a change to `shared/`, which every service depends on):

```bash
./build-and-push-images.sh
```

**Just one or a few**, by service directory name — much faster when only that service changed:

```bash
./build-and-push-images.sh orchestrator
./build-and-push-images.sh orchestrator web-adapter    # a few, space-separated
```

Valid names: `telegram-adapter`, `web-adapter`, `orchestrator`, `doc-ingestion-worker`, `doc-generation-worker`, `image-generation-worker` (an unknown name fails fast with that list, instead of silently building nothing). Combine with either mode:

```bash
./build-and-push-images.sh --build-only                    # build all 5, push none — sanity-check first
./build-and-push-images.sh --build-only orchestrator        # same, but just one service
DOCKERHUB_USER=someoneelse TAG=0.1.0 ./build-and-push-images.sh orchestrator   # override account/tag too
```

The script never touches your credentials — it just checks you're already logged in and tells you to run `docker login` if a push gets rejected.

Once the images are pushed, deploying/updating on the Barbara node is just re-pulling: `docker compose -f docker-compose.yml pull && docker compose -f docker-compose.yml up -d` (or however Barbara's own deployment flow triggers a pull — check the platform's docs for the exact mechanism on your node).

## Design notes

A few decisions worth knowing about before you start reading code:

- **`orchestrator` owns every external/shared connection, with one named exception.** MQTT, PostgREST, and the Gemini API (for the conversational flow) are only ever touched by `orchestrator`. The channel adapters (`telegram-adapter`, `web-adapter`) are the exception on the inbound/outbound side — each manages its own channel connection *and* its own MQTT connection, since they need to publish/subscribe on the bus directly as "external" services in their own right. `doc-ingestion-worker` has no MQTT or PostgREST connection at all — it talks to `orchestrator` over a small internal HTTP API (`shared/shared/internal_client.py`'s `InternalApiClient`, a thin `httpx` wrapper with bounded retry — deliberately *not* retry-forever like the MQTT reconnect loop, since these are one-off calls tied to something happening right now, not a long-lived connection worth resurrecting indefinitely).
- **`doc-ingestion-worker` keeps its own Gemini client — the one deliberate exception to full centralization.** Routing its vision-extraction call through `orchestrator` would mean `orchestrator` calls `doc-ingestion-worker` to extract → `doc-ingestion-worker` would have to call back into `orchestrator` just to reach Gemini → `orchestrator` would need to relay the result back to `doc-ingestion-worker` again. That's a circular hop for no benefit, so `doc-ingestion-worker` keeps a narrow, direct `GeminiClient` of its own, purely for vision extraction — everything else about its connectivity (no MQTT, no PostgREST) still follows the centralization rule.
- **No hand-rolled intent detection — Gemini drives everything via tool-use, orchestrator only executes.** There's no fixed set of flows and no branching on message type: every inbound message (text, an image, a document, a pasted URL, any combination) goes into one Gemini tool-use agent loop (`shared.gemini_client.GeminiClient.run_agent_loop`) with the tools in [`orchestrator/orchestrator/actions.py`](./orchestrator/orchestrator/actions.py) — `list_devices`, `get_device`, `get_compatible_devices`, `create_device`, `update_device`, `retire_device`, `attach_document`, `extract_device_data`, `generate_document`, `generate_image`, plus the built-in `google_search` grounding. Gemini decides which (if any) to call; content that's pure generation (a troubleshooting answer, a course, a replacement recommendation, the text of a generated report) is just its own final text or tool input, not a separate code path. A photo isn't automatically "a new device" — Gemini looks at it and decides (label → onboard it; error code on a screen → troubleshoot; documentation for something you already have → attach it).
- **A message with several attachments (e.g. a Telegram album) is handled one attachment at a time, chained across pause/resume cycles.** `telegram-adapter` buffers an album (Telegram delivers each photo as its own Update sharing a `media_group_id`) into a single `NormalizedMessage` with several `attachments`, and `orchestrator` walks through them sequentially — `extract_device_data` for one, its Human-in-the-Loop approval if it leads to a write, then the next — instead of trying to batch them (Gemini's own function-calling could ask for two pausing tools in the same turn, but `orchestrator` only ever resolves one at a time and `shared.gemini_client` refuses to silently auto-run the others, so a second write never slips past approval). `conversation.state.pending_attachments` keeps the original attachments available across however many round trips that takes.
- **A generated file is delivered as soon as it's rendered, as an attachment on the same channel.** `generate_document` asks Gemini to write the actual content first (a report, an export, whatever), then hands it to `doc-generation-worker` purely to render it into the requested format — the result comes back as a `shared.message.Attachment` on the reply, decoded and sent as a real file by whichever adapter is in play (`bot.send_document` on Telegram, a download link on the web chat).
- **An image request checks for one already on file before searching or generating a new one.** `generate_image`, given a `device_id`, first checks that device's attached documents (`home.device_document`, kind `photo`) and reuses one if it's still fetchable — only then does it fall through to `image-generation-worker` (a real Google Custom Search image lookup first, Gemini generation as the fallback). Either way the result is always a real JPEG, delivered via Telegram's `sendPhoto` (an inline preview, not a generic downloadable file — `AttachmentKind.IMAGE` vs. `AttachmentKind.DOCUMENT` on the `Attachment`).
- **Human-in-the-Loop approval gates every write.** `create_device`/`update_device`/`retire_device` pause the agent loop the same way the async vision-extraction/document-generation tools do, but instead of doing background work, `orchestrator` asks the user to approve or reject via the channel (Telegram inline buttons, or a plain sí/no reply elsewhere — see [`orchestrator/orchestrator/security_guard.py`](./orchestrator/orchestrator/security_guard.py)) before the call ever reaches PostgREST. Read-only tools (`list_devices`, `get_device`, `get_compatible_devices`) and the purely-additive `attach_document` execute immediately — no approval needed.
- **Conversation state is the Gemini transcript itself.** `conversation.state.history` is the literal list of messages replayed each turn — quiz progress, corrections mid-onboarding, which device a "yes" refers to are all just Gemini re-reading its own prior turns, not a hand-rolled state machine. Only one tool call is resolved per pause/resume round trip (see the multi-attachment note above) — no other tool in that batch runs, so a side-effecting call never executes only to be discarded.
- **Every conversation-state write is guarded against a concurrent one clobbering it.** `orchestrator/orchestrator/conversation.py::update_state` takes a `mutate(state) -> state` function, not a state dict built once from a snapshot — it applies `mutate` and writes with an `updated_at=eq.<last seen>` filter, retrying once against a freshly re-read row if another writer (a worker callback racing a fresh inbound message, or two callbacks) got there first. Two real incidents this project hit (a callback racing ahead of its own `pending_agent_turn` write) were specific cases of this general lost-update race — this closes the class, not just those two instances.
- **Descriptive while working, concise once it answers.** `telegram-adapter` shows Telegram's native "escribiendo..." indicator for as long as a turn is in flight, and `SYSTEM_PROMPT` (`orchestrator/orchestrator/llm.py`) asks Gemini for a one-sentence heads-up before any tool call that takes a moment — but the same prompt also asks it to keep the actual answer itself short and to the point, no preambles or wrap-up summaries.
- **No custom CRUD service.** Device/user/conversation data is served straight off Postgres by PostgREST — see [`db/schema.sql`](./db/schema.sql) for the schema and the `compatible_devices()` SQL function that powers the compatibility graph.
- **Nothing crashes on a connection hiccup.** Every service retries MQTT connections and missing credentials in a loop (with a configurable backoff) instead of exiting — see `shared/shared/mqtt_client.py` and `shared/shared/settings.py`.
- **`appconfig.json` hot-reloads, secrets don't.** Matches how Barbara's own connectors are configured — see any service's README for the exact split.
- **Single-tenant, single-household, no auth.** PostgREST runs a single service role with no JWT, and `web-adapter` has no login — this is a deliberate simplicity choice for a trusted home LAN, documented inline where it matters (and easy to revisit if this ever needs to serve more than one household).

## Operational runbook

There's no automated alerting for a conversation stuck mid-turn (waiting on a worker callback that already happened and was dropped, or one that genuinely never arrives) — the two real incidents that class of bug caused were both found by the household noticing the bot kept replying "Sigo con lo anterior..." A quick way to check for one directly against Postgres (or via PostgREST):

```sql
-- Conversations that have been "pending" for more than 10 minutes — worth
-- investigating (check the corresponding worker's logs for a dropped
-- callback) rather than assuming the household will always notice and report it.
SELECT id, channel, channel_conversation_id, state->>'pending_agent_turn' AS pending_tool_use_id, updated_at
FROM home.conversation
WHERE state ? 'pending_agent_turn'
  AND updated_at < now() - interval '10 minutes';
```

If one shows up stuck, clearing it directly (drop `pending_agent_turn`/`pending_attachments`/`pending_device_id`/`pending_confirmation` from `state`) unblocks the conversation — but consider appending a synthetic `function_response` to `state->'history'` for whatever tool call is still dangling first (matching what `pauses.py::finish_paused_turn` would have written), so Gemini doesn't see an unresolved tool call and confabulate a result for it on the next unrelated message.

## License

[MIT](./LICENSE) — do whatever you want with it.
