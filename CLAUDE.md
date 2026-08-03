# Project: Home AI Assistant on Barbara — CLAUDE.md

> This file is persistent project context. Claude Code should read it at the start of every session. The "Current state" section (at the end) gets updated every relevant session; the rest are decisions already made — don't reopen them unless something turns out to be impractical.

## Context

This document captures the design already decided during the architecture phase (a previous session with Claude Chat). The goal of this Claude Code session is to **start implementing**, not to re-discuss the design unless something turns out to be impractical in practice.

I'm a Solutions Architect at Barbara (an industrial edge computing platform). This project is a home project in its first phase, but it's deployed on Barbara with the stated intent to evolve it later into an industrial use case (a knowledge base for an industrial area: PLCs, Kepware, SCADA, MES, Historians). The design already accounts for that future evolution (see section 8), but **nothing industrial gets built now** — just keep the naming/schema conventions that leave the door open.

---

## 1. Functional goal (home phase — MVP)

A conversational assistant over Telegram that:
- Lets you onboard home devices (photos of labels/manuals → data extraction → confirmation → saved).
- Answers technical questions and helps troubleshoot issues (uses the device's spec sheet + web search if needed, with step-by-step guides).
- Generates on-demand "quick courses" on any topic, with a final quiz.
- Recommends replacements compatible with the rest of the devices at home (uses the compatibility graph + popularity/price search).
- Helps pick a new device that doesn't exist at home yet, prioritizing compatibility with what's already there.
- Lets you schedule reminders (maintenance, price drops, new firmware) and notifies you over Telegram when it's time.

**MVP scope (suggested build order):**
1. Device onboarding + troubleshooting Q&A (flows 1 and 2)
2. Scheduled reminders (the `notifier-scheduler` flow)
3. Quick course + quiz (flow 3)
4. Replacement / new purchase recommendations (flow 4)

---

## 2. Architecture decisions already made (don't reopen)

- **Microservices architecture on Docker Compose**, deployed as a Barbara node.
- **MQTT event bus** as the backbone — every service communicates by publishing/subscribing, never by calling each other directly.
- **Reuse Barbara Marketplace services instead of reimplementing them**: MQTT broker, PostgreSQL, InfluxDB, Grafana. Our own `docker-compose.yml` does **not** include these — it connects to them by hostname/Barbara's internal DNS.
- **Decoupled inbound channels via adapters**: Telegram is the first one, but any future channel (WhatsApp, Slack, voice, or — eventually — OT protocols) is just another adapter speaking the same message contract. The orchestrator never knows which channel a message came from.
- **User interface: Telegram** (bot via `aiogram` v3, **long polling** — see the tech stack and section 10).
- **Runtime**: Python for every service we own (consistent with the rest of the Barbara stack).

---

## 3. Architecture diagram

```
Telegram ──┐
Web       ─┼─→ [Channel adapter] ─→ MQTT (inbound) ─→ [Orchestrator] ─→ MQTT (outbound) ─→ [Adapter] ─→ Channel
WhatsApp/Slack*─┘                              │
                                                ├─→ Postgres  (devices, users, conversations)
                                                ├─→ InfluxDB  (prices, time-series metrics)
                                                ├─→ Claude API (reasoning + tools)
                                                └─→ Grafana   (health/usage dashboards)

[Scheduler/Notifier] ─→ MQTT (events) ─→ [Orchestrator] ─→ MQTT (outbound) ─→ Adapter ─→ Channel

(* future, not part of this MVP — Web isn't "future" anymore, it got moved up: see section 10)
```

---

## 4. Microservices to build (own docker-compose)

| Service | Responsibility | Implementation notes |
|---|---|---|
| `telegram-adapter` | Translates Telegram ↔ normalized message schema (section 5) | aiogram v3, long polling; publishes/consumes MQTT |
| `web-adapter` | Minimal web chat channel (always available, no Telegram dependency) ↔ normalized message schema | aiohttp + WebSocket, self-contained static page, no login (trusted LAN); the only own service that exposes a port (8090) |
| `orchestrator` | Receives the normalized message, manages session/context, decides intent (device onboarding / question / course / replacement), calls the Claude API with tools, reads/writes via PostgREST | Must be **stateless** — conversation state lives in Postgres (via PostgREST), not in process memory, so it can scale to replicas |
| `doc-ingestion-worker` | Receives photos of labels/manuals, extracts data via Claude, normalizes and saves via PostgREST | Its own queue — must not block the normal chat flow (this can take a while) |
| `notifier-scheduler` | Checks maintenance reminders, watches prices/firmware, publishes events to the bus when it's time to notify | Cron-like (APScheduler); publishes directly to `home/outbound/<channel>` if the alert is deterministic; goes through the orchestrator only if it needs Claude to word/reason about it |

> **`device-registry-api` was dropped as its own microservice.** Device/taxonomy/location/user CRUD, plus the compatibility graph (via the `home.compatible_devices` SQL function), are served directly by **PostgREST** (a Barbara Marketplace service) over the `home` schema — see `db/schema.sql` and section 7. `orchestrator` and `doc-ingestion-worker` are HTTP clients of PostgREST, not of a custom API.

---

## 5. Message contract (MQTT payload)

Every channel adapter publishes/consumes this schema, whatever the channel:

```json
{
  "channel": "telegram",
  "user_id": "12345",
  "conversation_id": "uuid",
  "type": "text | photo | command",
  "content": "...",
  "attachments": ["url_or_base64"],
  "timestamp": "iso8601"
}
```

> Note for the future (don't implement now): leave conceptual room for a `telemetry/event` type that would let PLC/Historian data flow through the same bus in the industrial evolution, without a human writing it.

### MQTT topic naming

```
home/inbound/<channel>/<user_id>
home/outbound/<channel>/<user_id>
home/events/reminder
home/events/price_alert
home/events/firmware_update
```

---

## 6. The 4 conversation flows (functional detail)

1. **Device onboarding**: user sends a photo of a label/manual → `doc-ingestion-worker` calls Claude (vision) to extract structured data (brand, model, specs, category) → shown to the user to confirm/correct → saved via **PostgREST** (see the note right above — no custom registry API).
2. **Troubleshooting**: `orchestrator` fetches the relevant device's spec sheet from Postgres, decides whether it needs `web_search` (via a Claude tool) for more info (online manuals, forums, known fixes) → returns a step-by-step guide.
3. **Quick course + quiz**: `orchestrator` generates educational content on the requested topic plus a quiz; saves the correct answers in the conversation session (Postgres); grades it at the end and gives feedback.
4. **Replacement / new purchase**: `orchestrator` queries the compatibility graph via PostgREST (`home.compatible_devices`) + uses `web_search` for popularity/pricing → returns ranked options with cited sources.

---

## 7. Data schema — principles (Postgres)

Design the tables with these conventions from the start (they're free now, expensive to add later):

- **`tenant_id` / `site_id` on every relevant table**, even though today it's always `"home"` — sets up multi-tenancy without a future rewrite.
- **Device attributes in JSONB**, not rigid per-type columns — lets you add asset types (appliances today, PLCs/drives tomorrow) without a schema migration.
- **A device-type taxonomy table**, separate from the instances table.
- **Location hierarchy modeled even though it's flat today** (e.g. a `location_path` like `home/kitchen` — designed to scale to `plant/area/line/cell`).
- **A users table with role + scope**, even though today there's only one user with a single role.
- **Conversations and their state in their own table** (not in the orchestrator's memory) — needed so `orchestrator` can scale to replicas.
- **Compatibility graph**: model it as a table of device↔device relationships (or device↔standard/protocol, e.g. "Zigbee", "Matter") instead of free-text fields — that's what lets it answer "what's compatible with what I already have" well.

The full DDL isn't required in this brief — it can be defined in the first Claude Code session, starting from these principles.

---

## 8. Ready for an eventual industrial use case (context, not current scope)

This project could evolve into a Barbara use case for an industrial area's knowledge base (PLCs, Kepware, SCADA, MES, Historians instead of Telegram/appliances), with analogous use cases but much bigger data volumes. **None of that gets built now.** The only practical implication for the current MVP is the section 7 conventions (JSONB, taxonomy, `tenant_id`, hierarchy, user role/scope) and the "one worker per data source" pattern already adopted in `doc-ingestion-worker`.

---

## 9. Target hardware (deployment context, not a blocker for development)

- Will run on a Barbara node on **Ubuntu**, with **Docker** as the container engine.
- Other services will run in parallel on the same machine (Barbara Marketplace services + possible future ones).
- Recommended hardware: **TEGA TBOX-2 Series** (fanless, Core CPU variant, 8-16 GB RAM, 128-256 GB SSD/NVMe) — or a cheaper x86 mini PC (Intel N100/N200) if industrial certification isn't required.
- Target architecture: **x86_64** (better Docker compatibility than ARM).
- No GPU needed — the reasoning/inference happens on Claude's API in the cloud; the node just orchestrates.

---

## 10. Current project state

> Update this section at the end of every relevant Claude Code session. Keep it as a **snapshot of the current state** (what's there and how it works), not a chronological session log — the history of "why we got here" lives in the conversation/commit history, not here.

**Phase:** Functional end-to-end MVP, now built around a Claude-driven tool-use agent loop (see below) instead of 4 fixed flows, **plus** completed internal-communications/configuration/deployment redesigns from earlier sessions, not yet tested against real infrastructure (broker/Postgres/PostgREST/Claude) — only compiled, linted, and tested with mocks (respx for HTTP, aiohttp TestClient/TestServer, AsyncMock/MagicMock for PostgREST/MQTT).

### LLM engine made pluggable — Gemini is now the active default (latest session)

The "brain" is no longer hardcoded to Anthropic. New `shared/shared/engines/` package: `base.py` defines a provider-agnostic `Engine` protocol (`build_user_message`, `find_tool_use`, `run_agent_loop`, `resume_agent_loop`, `call_structured`); `anthropic_engine.py` holds the original Claude implementation (moved from the now-deleted `shared/shared/claude.py`, unchanged in substance); `gemini_engine.py` is new, using `google-genai`. `get_engine(name, service_name, model_name, retry_seconds)` is the one factory both `orchestrator/claude_client.py` and `doc-ingestion-worker/extractor.py` call — nothing else in the codebase imports `anthropic`/`google.genai` directly anymore.

- **Selected via appconfig**: `"engine": "gemini"` (default) or `"anthropic"`, plus `"model"` (default `gemini-flash-latest` / `claude-sonnet-5`) — both hot-reload-visible but only read at engine construction (process start), same as any other secret-adjacent setting.
- **New secret**: `GEMINI_API_KEY` in `barbarasecrets.env`, alongside `ANTHROPIC_API_KEY` (kept — Anthropic is still a selectable engine, just not the active one).
- **Deliberate v1 simplifications on the Gemini engine** (see its module docstring): no prompt-caching equivalent; web search maps to Gemini's `google_search` grounding tool (no separate "fetch this URL" tool like Anthropic's `web_fetch`); every attachment is downloaded and sent as inline bytes (Gemini has no `url`-source content block at all, unlike Claude's `image` blocks).
- **`tools.py`'s `TOOL_SCHEMAS` didn't change** — plain JSON Schema, engine-agnostic; each engine converts it internally (Anthropic: used as-is; Gemini: wrapped into `FunctionDeclaration(parameters_json_schema=...)`).
- **Verified against the live Gemini API against real infrastructure**: the initial default, the pinned model name `gemini-2.5-flash`, turned out to be 404 "no longer available to new users" despite still appearing in `client.models.list()` — list-membership doesn't imply invocability for a given account/tier. Fixed by test-invoking candidates directly and switching the default to `gemini-flash-latest`, a Google-maintained alias that always points at their current flash-tier model — avoids this exact class of breakage recurring from a hardcoded dated model name.

### Conversational core rebuilt: Claude drives via tool-use (previous session)

The 4 fixed flows, `classify_intent`, and the `pending_action` state machine are gone. Replaced by a single Claude tool-use agent loop (`shared.claude.run_agent_loop`/`resume_agent_loop`, new) that `orchestrator` runs on every inbound message — Claude decides everything itself (onboard a device, edit one, attach documentation, troubleshoot, teach a course, recommend a replacement, schedule a reminder) by calling tools; orchestrator only executes what it's told and manages state/notifications (see the root README's design notes for the full rationale).

- **The tool surface** (`orchestrator/orchestrator/tools.py`): `list_devices`, `create_device`, `update_device` (new), `attach_document` (new), `extract_device_data`, `schedule_reminder` (new — closes known-gap #1), plus the built-in `web_search`/`web_fetch` server tools, now the `_20260209` dynamic-filtering variants (closes known-gap #4, verified against current `anthropic` SDK docs this session). Pure content generation — troubleshooting answers, course lessons+quizzes, replacement recommendations — is **not** a tool; it's just Claude's own final text, using `list_devices`/`web_search`/`web_fetch` as needed. `webSearchEnabled` (appconfig) still gates both server tools together, read live on every turn (`main.py::_tools()`), same hot-reload behavior as before.
- **Conversation state is now just the Claude message transcript** (`conversation.state.history`) plus, transiently, `pending_agent_turn` (a boolean gate) while the one async tool is in flight. The old `pending_action`/`draft_device`/`quiz_questions`/`quiz_index`/`quiz_correct` keys are gone — quiz progress, corrections mid-onboarding, and confirmations are just Claude re-reading its own prior turns, not a hand-rolled state machine.
- **`extract_device_data` is the one asynchronous tool** — kicked off by `main.py` (not inside the loop), since it needs an HTTP round trip to `doc-ingestion-worker` that can't be awaited inline. When Claude calls it, `run_agent_loop` returns `done=False` *without executing any tool in that batch* (so a side-effecting call like `create_device` never runs only to be discarded); `main.py` fires `POST /extract`, persists `pending_agent_turn`, and resumes via `resume_agent_loop` when `POST /internal/doc-ingestion/result` calls back — same endpoint as before, new internals. A failed extraction is now handed to Claude as a normal tool result (`{"success": false, "error": ...}`) instead of a hardcoded Python reply, so it can naturally ask for a clearer photo.
- **Prompt caching from day one**: `run_agent_loop` sets a `cache_control` breakpoint on the system prompt (which also caches the tool schema list, since render order is tools → system → messages) and on the last content block of the growing history — 2 of the max 4 breakpoints, unconditionally on every call.
- **Parallel tool calls execute concurrently** (`asyncio.gather`) and come back as one combined `tool_result` message — never split across messages, per Anthropic's own guidance that splitting trains the model to stop parallelizing.
- **New `home.device_document` table** (`db/schema.sql`) backs `attach_document` — manuals, label photos, or free-form notes attached to an existing device.
- **`home.app_user` finally gets rows** — closes known-gap #3 as a side effect: `schedule_reminder` upserts the calling user's `(channel, channel_user_id)` the first time anyone schedules a reminder.
- **Message contract changed**: `NormalizedMessage.attachments` is now `list[Attachment]` (`kind`: image/document/audio, `media_type`, `filename`), not `list[str]`; `MessageType.PHOTO` is gone — a message is text plus zero-or-more attachments now, never "one type". `telegram-adapter` now also forwards Telegram documents, not just photos; `web-adapter`'s upload form generalized from photo-only to any file, mapped to `image`/`document` by MIME type.
- **Audio is modeled, not wired up**: `AttachmentKind.AUDIO` exists so the contract has room, but no adapter captures it and `shared.claude.build_content_blocks` just emits a placeholder text note for it — Claude's Messages API has no audio content-block type. Revisit only if the household needs it.
- **New Claude content-block helpers** in `shared/shared/claude.py`: `image_block()` (also now reused by `doc-ingestion-worker`'s extractor, replacing its own private copy) and `build_content_blocks()`. A `document`-kind attachment that's still a URL (e.g. a Telegram document) gets downloaded and base64-encoded before being sent — unlike `image`, Claude's `document` content block has no `url` source type.
- **Deleted**: `orchestrator/orchestrator/flows/` (all 4 modules) and most of `claude_client.py` (`classify_intent`, `interpret_confirmation`, `generate_course`, `interpret_quiz_answer`, `ask_troubleshooting`, `ask_replacement`, and their Pydantic result models) — only `word_reminder()` remains there (proactive notifications aren't a conversation turn, so they stay a narrow direct call) alongside the new `SYSTEM_PROMPT`.
- **Verified functionally** (mocks, not real infra): the agent loop's scripted tool-call sequences, parallel-execution batching, cache_control placement, the pause/resume cycle end-to-end (including via `aiohttp` `TestClient` against `handle_doc_ingestion_result`), `handle_inbound` for a plain question, a photo Claude chooses to onboard, a photo Claude chooses to treat as something else entirely (proving the old unconditional "photo → onboarding" branch is really gone), and a reminder request that creates both `home.app_user` and `home.reminder`.

### Internal/external communications redesign (earlier session)

`orchestrator` is now the **single owner of every external/shared connection** — MQTT, PostgREST, and the Claude API for all conversational flows — with two categories of named exception:

- **Channel adapters** (`telegram-adapter`, `web-adapter`) keep their own MQTT connection *and* their own channel connection (Telegram/WebSocket) — they're "external" services in their own right, publishing/subscribing on `home/inbound/<channel>/*` and `home/outbound/<channel>/*` directly. Unchanged by this redesign.
- **`doc-ingestion-worker`** keeps its own `AsyncAnthropic` client, purely for vision extraction — the one deliberate exception to full centralization, to avoid a circular HTTP hop (`orchestrator` → this service → back to `orchestrator` for Claude → back to this service). It otherwise has **no MQTT, no PostgREST** — reached via `orchestrator`'s internal HTTP API instead.

Everything else that isn't a channel adapter now talks to `orchestrator` over a small internal HTTP API (`shared.internal_client.InternalApiClient` — thin `httpx` wrapper, bounded retry, returns `None` on total failure instead of raising):

| Endpoint (on `orchestrator`, port `8080`, not host-exposed) | Called by | Replaces |
|---|---|---|
| `POST /extract` (on `doc-ingestion-worker`, port `8080`) | `orchestrator` | the old `home/events/doc_ingestion` MQTT topic |
| `POST /internal/doc-ingestion/result` | `doc-ingestion-worker` | the old `home/events/doc_ingestion_result` MQTT topic |
| `POST /internal/reminders/check` | `notifier-scheduler` | `notifier-scheduler`'s old direct PostgREST/MQTT reminder logic |

Other changes that came with this redesign:
- **All Claude calls are now async** (`AsyncAnthropic` instead of `Anthropic`, `call_structured()` is now `async def`) — centralizing every conversational call into one process means a blocking synchronous call would stall every conversation in the house, not just one process's worth of work.
- **`bootstrap_service()` no longer auto-loads MQTT secrets** — not every service has an MQTT connection anymore. It now returns `(system, appconfig)`; each service's `config.py` explicitly calls `load_secrets()` for whatever it actually needs (`MqttSecrets` only for `orchestrator`/`telegram-adapter`/`web-adapter`; new `OrchestratorSecrets`/`DocIngestionWorkerSecrets` for the internal-API URLs).
- **`notifier-scheduler` lost its reminder logic entirely** — it's now a bare cron heartbeat that pings `/internal/reminders/check`. The logic (fetch due reminders, dispatch, word via Claude, reschedule/mark-sent) moved to `orchestrator/orchestrator/reminders.py`. It still exists as a separate service solely because it owns a Postgres-backed `APScheduler` jobstore that needs to survive independently of `orchestrator`'s process lifecycle.
- **Closed a previously-dead gap as a side effect**: the "reminder needs Claude to word it nicely" path (previously an MQTT event nobody consumed — see old known-gap #2 below) is now a real code path, `word_reminder()` in `orchestrator/orchestrator/claude_client.py`, called from `dispatch_reminder()`.

### Configuration made 100% Barbara-compatible (earlier session)

Switched from one `appconfig.json`/`.env.example` **per service** to the exact single-file convention used by Barbara's own [`boilerplate_01_python`](https://github.com/Barbaraedge/training_barbara_apps_development/tree/main/boilerplate_01_python) reference project — one appconfig, one secrets file, for the **whole app**, not one per docker-compose service (this is genuinely how Barbara Secrets/Appconfig work on a real node: one store per app/project, injected identically into every container):

- **`appconfigDev/appconfig.json`** (repo root) replaces the 5 per-service `appconfig.json` files — one JSON, one top-level key per service, merged from what used to be scattered across the repo. Mounted read-only at `/appconfig/appconfig.json` in every container (was `/app/appconfig.json`, one per service).
- **`appconfigDev/global.json`** (repo root) is new — Barbara's "Appconfig Device Level", mounted at `/appconfig/global.json`. Not consumed by any service yet; `shared/shared/settings.py` now has `load_global_config()` ready for whenever something needs it.
- **`barbarasecrets.env`** (repo root) replaces the 5 per-service `.env.example` files — one env file with every service's secrets (MQTT/Anthropic/PostgREST/etc.), deduplicated (e.g. one set of `MQTT_*` vars, not five copies), injected identically into every container via `env_file:` in `docker-compose-local.yml`.
- **`shared/shared/settings.py`**: `load_appconfig`, `load_service_config`, `bootstrap_service`, and `watch_appconfig` all default to `/appconfig/appconfig.json` now (was `/app/appconfig.json`); no service's `config.py` needed changes, since none of them overrode the default path.
- **`docker-compose-local.yml`**: every service's `env_file`/`volumes` now point at the shared root files (`barbarasecrets.env`, `./appconfigDev:/appconfig:ro`) instead of its own folder. `docker-compose.yml` (the Barbara-node file) needed no changes — it never declared per-service `volumes`/`env_file` to begin with, since the platform already injects both at these same paths.
- The 5 per-service `appconfig.json` and `.env.example` files are deleted — no longer used anywhere.

### Deployment now pulls pre-built images from Docker Hub (earlier session)

`docker-compose.yml` (the Barbara-node file) no longer has any `build:` block — each service is now `image: kikeramirez/home-assistant-<service>:latest`, one Docker Hub repository per service (`home-assistant-telegram-adapter`, `home-assistant-web-adapter`, `home-assistant-orchestrator`, `home-assistant-doc-ingestion-worker`, `home-assistant-notifier-scheduler`). **`docker-compose-local.yml` is deliberately untouched** — local debugging always builds from `Dockerfile.service` against current source, per explicit instruction; the two compose files no longer look structurally identical aside from image vs. build, and that's intentional now, not drift to fix.

- **`build-and-push-images.sh`** (repo root, new) — loops over the same 5 `SERVICE`/`MODULE` pairs `Dockerfile.service`'s build args expect, builds each with `docker build -f Dockerfile.service --build-arg SERVICE=... --build-arg MODULE=... -t <user>/home-assistant-<service>:<tag> .`, then pushes. Takes optional service-name arguments (`telegram-adapter`, `web-adapter`, `orchestrator`, `doc-ingestion-worker`, `notifier-scheduler`) to build/push just one or a few instead of all 5 — no args means all 5; an unrecognized name fails fast rather than silently building nothing. Configurable via `DOCKERHUB_USER`/`TAG` env vars (defaults `kikeramirez`/`latest`); `--build-only` skips the push (composable with a service-name filter). Never calls `docker login` itself — checks `docker info` for a logged-in user and warns if there isn't one, but leaves the actual credential entry to the user, always interactive. Docker Hub URLs for each repo are in the root README's deployment section.
- **Docker access was fixed and the images are published.** Docker Desktop's WSL integration wasn't enabled for this distro at first (the `docker` CLI wasn't reachable from this shell); the user enabled it, logged in interactively (`docker login`), and `./build-and-push-images.sh` was run for real — all 5 images built and pushed successfully, confirmed present on Docker Hub via `docker manifest inspect` for each of `kikeramirez/home-assistant-{telegram-adapter,web-adapter,orchestrator,doc-ingestion-worker,notifier-scheduler}:latest`. `docker-compose.yml` is ready to pull them on a real node.

### Repo structure

Monorepo with `uv` workspaces. One package per service + `shared/`:

| Package | Role |
|---|---|
| `shared` | Message contract incl. `Attachment`/`AttachmentKind` (`message.py`), PostgREST client over `postgrest-py` (`postgrest_client.py`), MQTT client/reconnection over `aiomqtt` (`mqtt_client.py`), internal HTTP client over `httpx` (`internal_client.py`), secrets/appconfig/logging/hot-reload (`settings.py`), structured-extraction call + the agent loop + Claude content-block builders (`claude.py`: `call_structured`, `run_agent_loop`/`resume_agent_loop`, `image_block`/`build_content_blocks`) |
| `telegram-adapter` | Telegram channel — `aiogram` v3, long polling (no port, no webhook); owns its own MQTT connection; forwards photos and documents as `Attachment`s |
| `web-adapter` | "Always available" web chat channel (no Telegram dependency) — `aiohttp` + WebSocket, self-contained static page at `/`, the only service with a host-exposed port (8090); owns its own MQTT connection; generic file upload (any type, mapped to `image`/`document` by MIME) |
| `orchestrator` | The conversational brain: runs Claude's tool-use agent loop (`tools.py` + `shared.claude.run_agent_loop`) on every inbound message — no intent branching of its own, stateless (state in `home.conversation` via PostgREST). The only service connected to MQTT/PostgREST/Claude besides the two adapters; also runs the internal HTTP API (port 8080, not host-exposed) described above |
| `doc-ingestion-worker` | Extracts data from label photos via Claude vision (invoked by the `extract_device_data` tool, not automatically), with bounded concurrency. No MQTT/PostgREST — reached via `POST /extract`, calls back via `POST /internal/doc-ingestion/result`. Keeps its own `AsyncAnthropic` client (the one exception) |
| `notifier-scheduler` | `APScheduler` + Postgres jobstore, nothing else — pings `orchestrator`'s `POST /internal/reminders/check` on a timer |

`docker-compose.yml` (Barbara node: no `volumes`/`env_file`, the platform injects them; pulls pre-built `image:`s from Docker Hub, no `build:` — see the deployment subsection above) and `docker-compose-local.yml` (local debugging: every service mounts the shared `appconfigDev/` and `barbarasecrets.env` at the repo root, and builds from `Dockerfile.service` parameterized via build args `SERVICE`/`MODULE` — there's no per-service Dockerfile) intentionally diverge on build-vs-image now, but both use the `barbaraServices` network (`driver: bridge`); `orchestrator`'s and `doc-ingestion-worker`'s internal ports need no `ports:` mapping since they're reachable over the network's internal Docker DNS (only `web-adapter` needs a host-exposed port). Root `README.md` + a `README.md` per package, plus a root `LICENSE` (MIT) — see the next subsection.

### The agent loop and its tools (in `orchestrator`)

There's no flow dispatch anymore — `main.py::handle_inbound` builds the new turn's content blocks, appends them to `conversation.state.history`, and runs `shared.claude.run_agent_loop` with the tool list from `tools.py`. **No interpretation of what the user says uses our own keyword-matching or a fixed intent enum** — Claude decides everything by choosing which tool(s) to call, or none at all.

- **Device onboarding**: user sends a photo → Claude decides it's a label/manual and calls `extract_device_data` (the one async tool, resolved via `doc-ingestion-worker`'s `/extract` + `POST /internal/doc-ingestion/result` callback, same as before) → Claude reviews the draft with the user, correcting fields inline if needed (no rigid yes/no gate) → calls `create_device`.
- **Editing an existing device**: `update_device`, called directly when the user corrects or adds detail — previously not possible at all (no update path existed).
- **Attaching documentation**: `attach_document` — a manual, a label photo, or a note saved against an existing device (`home.device_document`, new table). Distinguishing "new device" from "documentation for one that exists" is Claude's judgment call, not a Python branch on message type.
- **Troubleshooting / course+quiz / replacement recommendations**: not tools at all — pure Claude-generated final text, using `list_devices` (with or without standards) and `web_search`/`web_fetch` as needed. Quiz grading is Claude re-reading its own prior turns from `conversation.state.history`, not a tracked `quiz_index`/`quiz_correct` state.
- **Reminders**: `schedule_reminder` inserts into `home.reminder` (and upserts `home.app_user` for the caller, first use closes known-gap #3) whenever the user asks to be reminded about something.

### Configuration, logging, and resilience (`shared/settings.py`, `shared/mqtt_client.py`)

- **Secrets** (`barbarasecrets.env`, one file for the whole app) vs. **appconfig** (`appconfigDev/appconfig.json`, one file for the whole app, matching Barbara's real shape: `{"<service>": {"system": {"debugLevel", "connectTimeoutMs"}, "otherParam": ...}}`) vs. **global config** (`appconfigDev/global.json`, device-level, not consumed yet) — see the configuration subsection above for why these are single, repo-wide files rather than one per service.
- **No service ever stops over a connection/configuration problem**, whether at startup or while running: `load_secrets` retries in a loop (never `sys.exit`) if a credential is missing, naming the exact variable; `maintain_mqtt_connection` reconnects forever with a backoff of `connectTimeoutMs`. In the channel adapters, MQTT lives in a background task independent of the channel itself (Telegram/HTTP keep working even while MQTT is down).
- **`appconfig.json` hot-reloads** every 10s (`watch_appconfig`), reading `/appconfig/appconfig.json` — no restart needed — secrets always require a restart. Real exceptions: `web-adapter`'s `port`, and `orchestrator`'s and `doc-ingestion-worker`'s internal-API `port` — none of them can be re-bound without a restart.
- **`bootstrap_service(name)`** centralizes each `config.py`'s startup (appconfig → logging) into a single call, defaulting to `/appconfig/appconfig.json` — it no longer auto-loads MQTT secrets (see redesign notes above); each service loads exactly the secrets it needs afterward.
- Known, accepted edge cases: in `doc-ingestion-worker`, a request already being processed when the container dies loses its result (not retried, no persistent queue); in `web-adapter`, outbound messages are dropped if the browser tab is closed (it's the fallback/debugging channel, not the primary one); `InternalApiClient`'s bounded retry means a transient blip while `orchestrator` is restarting can drop one `doc-ingestion` callback or one `reminders/check` tick — acceptable for a home project, the next scheduled tick or user action recovers.

### Data schema (`db/schema.sql`)

Schema `home`, a single `app_service` role (no JWT — see the comment in the file itself), RLS enabled (policy by `tenant_id`, always `'home'` today). Tables: `device_type`, `standard`, `device` (with ISA95 columns), `device_standard`, `device_compatibility`, `device_document` (new — backs the `attach_document` tool), `app_user`, `conversation`, `message`, `reminder`; a `home.compatible_devices(uuid)` function for the compatibility graph.

> Note: `home.message`, `home.device_compatibility`, `device_type.attributes_schema`/`parent_type_id`, and `device.owner_user_id` are defined but nothing uses them yet — that's deliberate design from the original brief (section 7/8, leaving cheap room now), not dead code; it's an open decision whether to trim them at some point. (`conversation.state.history` — the Claude message transcript — is what the agent loop actually replays each turn; `home.message` staying unused is a deliberate choice, not an oversight, since duplicating that transcript there wouldn't currently add anything.)

### Documentation, licensing, and language

- **Public-facing docs**: a root `README.md` (functionality, architecture diagram, service table, external dependencies, local run instructions) plus one `README.md` per service/package (config, secrets, how to get external credentials — Telegram bot token, Anthropic API key). `LICENSE` is MIT, chosen specifically so the repo can be shown publicly.
- **Repo language policy**: all code (comments, docstrings, log messages), the DDL, `CLAUDE.md`, and every README are in English. Reasoning/system prompts sent to Claude are English too.
- **Explicit exception**: the handful of fixed reply strings orchestrator still sends directly (in `main.py` — pending-turn/failure notices around the async extraction; in `shared/claude.py` — the `max_iterations`-exhausted fallback) and the `web-adapter` static UI (`static/index.html`) are intentionally left in Spanish — that's the household's real spoken language, and replies are meant to mirror whatever language the user writes in, not be forced into English. Every other reply is Claude-generated now (there's no more per-flow reply-string code), and `SYSTEM_PROMPT` in `claude_client.py` explicitly instructs Claude to reply in the same language the user wrote in, so that part already works correctly regardless of what language the prompt itself is written in.

### Known gaps (not blocking)

1. ~~Nothing creates rows in `home.reminder`~~ — **closed** by this session: the `schedule_reminder` tool inserts directly whenever the user asks to be reminded about something.
2. ~~`orchestrator` doesn't subscribe to the reminder-wording event~~ — **closed** by an earlier session's redesign: `dispatch_reminder()` in `orchestrator/orchestrator/reminders.py` calls `word_reminder()` (Claude) directly whenever a due reminder has no ready-made `payload.message`, no MQTT event involved anymore.
3. ~~Nothing creates rows in `home.app_user`~~ — **closed** by this session: `schedule_reminder` upserts `(channel, channel_user_id)` for the calling user the first time anyone schedules a reminder.
4. ~~Verify the `web_search_20250305` tool name/version~~ — **closed** by this session: upgraded to the `_20260209` dynamic-filtering variants of `web_search`/`web_fetch` (confirmed current and Sonnet-5-compatible against the `anthropic` SDK docs), and added `web_fetch` as a new tool alongside `web_search`.
5. ~~The parameterized `Dockerfile.service` hasn't been tested with a real Docker build~~ — **closed**: Docker Desktop's WSL integration got enabled for this distro, and `build-and-push-images.sh` has now actually built and pushed all 5 images for real (see the deployment subsection above) — the Dockerfile/build-arg pattern is confirmed working, not just syntax-checked.
6. The handful of remaining fixed deterministic reply strings (the async-extraction pending/failure notices in `main.py`, the `max_iterations` fallback in `shared/claude.py`) have no per-message language detection wired up — they stay in Spanish regardless of what language the user actually wrote in, unlike every Claude-generated reply. Only worth fixing if the household ever needs the assistant to work in more than one language day-to-day. Much smaller surface than before this session, since almost every reply is Claude's own text now.
7. `resume_agent_loop` doesn't support a second `extract_device_data` call within the same resumed turn (e.g. a message with more than one photo, both meant to become new devices) — `handle_doc_ingestion_result` degrades gracefully (asks the user to send them one at a time) rather than hanging, but it's a real gap if that scenario ever comes up in practice.

**Pending (functional, suggested order):** verification against real infrastructure (broker/Postgres/PostgREST/Claude on an actual node, including the new internal HTTP API between `orchestrator`/`doc-ingestion-worker`/`notifier-scheduler`, and an actual `docker compose -f docker-compose.yml pull && up -d` against the now-published images) — the images on Docker Hub predate this session's redesign, so they need rebuilding/pushing again before that verification (`./build-and-push-images.sh`).

**Blockers/open decisions:** none.
