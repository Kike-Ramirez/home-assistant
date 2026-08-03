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

1. **Device onboarding**: user sends a photo of a label/manual → `doc-ingestion-worker` calls Claude (vision) to extract structured data (brand, model, specs, category) → shown to the user to confirm/correct → saved via `device-registry-api`.
2. **Troubleshooting**: `orchestrator` fetches the relevant device's spec sheet from Postgres, decides whether it needs `web_search` (via a Claude tool) for more info (online manuals, forums, known fixes) → returns a step-by-step guide.
3. **Quick course + quiz**: `orchestrator` generates educational content on the requested topic plus a quiz; saves the correct answers in the conversation session (Postgres); grades it at the end and gives feedback.
4. **Replacement / new purchase**: `orchestrator` queries the compatibility graph in `device-registry-api` + uses `web_search` for popularity/pricing → returns ranked options with cited sources.

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

**Phase:** Functional end-to-end MVP (4 flows + notifier-scheduler), not yet tested against real infrastructure (broker/Postgres/PostgREST/Claude) — only compiled, linted, and tested with mocks.

### Repo structure

Monorepo with `uv` workspaces. One package per service + `shared/`:

| Package | Role |
|---|---|
| `shared` | MQTT message contract (`message.py`), PostgREST client over `postgrest-py` (`postgrest_client.py`), MQTT client/reconnection over `aiomqtt` (`mqtt_client.py`), secrets/appconfig/logging/hot-reload (`settings.py`), structured Claude call helper (`claude.py`) |
| `telegram-adapter` | Telegram channel — `aiogram` v3, long polling (no port, no webhook) |
| `web-adapter` | "Always available" web chat channel (no Telegram dependency) — `aiohttp` + WebSocket, self-contained static page at `/`, the only service with an exposed port (8090) |
| `orchestrator` | The conversational brain: routes by channel/intent, runs the 4 flows, stateless (state in `home.conversation` via PostgREST) |
| `doc-ingestion-worker` | Extracts data from label photos via Claude vision, with bounded concurrency |
| `notifier-scheduler` | `APScheduler` + Postgres jobstore; checks `home.reminder` and publishes alerts |

`docker-compose.yml` (Barbara node: no `volumes`/`env_file`, the platform injects them) and `docker-compose-local.yml` (local debugging: keeps them) share a single `Dockerfile.service` parameterized via build args (`SERVICE`, `MODULE`) — there's no per-service Dockerfile. Both use the `barbaraServices` network (`driver: bridge`).

### The 4 flows (in `orchestrator`)

With no pending conversation state, `classify_intent` (Claude) decides which flow to go to. **No interpretation of what the user says uses our own keyword-matching** — it all goes through Claude with structured output (`shared.claude.call_structured`: `tool_choice` + Pydantic + retry if the schema isn't met).

1. **Device onboarding** (`flows/onboarding.py`): photo → `doc-ingestion-worker` (Claude vision, same `call_structured`) → confirmation (`interpret_confirmation`, Claude) → saved via PostgREST.
2. **Troubleshooting** (`flows/troubleshooting.py`): the full device inventory is passed to Claude (no heuristic pre-filtering) + `web_search` if needed.
3. **Course + quiz** (`flows/course.py`): Claude generates the lesson and questions; every user answer is interpreted by Claude (`interpret_quiz_answer`), not a letter/number parser.
4. **Replacement / purchase** (`flows/replacement.py`): inventory + standards (PostgREST resource embedding) + `web_search` → ranked recommendation with sources.

### Configuration, logging, and resilience (`shared/settings.py`, `shared/mqtt_client.py`)

- **Secrets** (env vars) vs. **appconfig** (`appconfig.json`, matching Barbara's real shape: `{"<service>": {"system": {"debugLevel", "connectTimeoutMs"}, "otherParam": ...}}`).
- **No service ever stops over a connection/configuration problem**, whether at startup or while running: `load_secrets` retries in a loop (never `sys.exit`) if a credential is missing, naming the exact variable; `maintain_mqtt_connection` reconnects forever with a backoff of `connectTimeoutMs`. In the channel adapters, MQTT lives in a background task independent of the channel itself (Telegram/HTTP keep working even while MQTT is down).
- **`appconfig.json` hot-reloads** every 10s (`watch_appconfig`), no restart needed — secrets always require a restart. One real exception: `web-adapter`'s `port` can't be re-bound without a restart.
- **`bootstrap_service(name)`** centralizes each `config.py`'s startup (appconfig → logging → MQTT secrets) into a single call.
- Known, accepted edge cases: in `doc-ingestion-worker`, a photo already being processed when MQTT drops loses its result (not retried); in `web-adapter`, outbound messages are dropped if the browser tab is closed (it's the fallback/debugging channel, not the primary one).

### Data schema (`db/schema.sql`)

Schema `home`, a single `app_service` role (no JWT — see the comment in the file itself), RLS enabled (policy by `tenant_id`, always `'home'` today). Tables: `device_type`, `standard`, `device` (with ISA95 columns), `device_standard`, `device_compatibility`, `app_user`, `conversation`, `message`, `reminder`; a `home.compatible_devices(uuid)` function for the compatibility graph.

> Note: `home.message`, `home.device_compatibility`, `device_type.attributes_schema`/`parent_type_id`, and `device.owner_user_id` are defined but no flow uses them yet — that's deliberate design from the original brief (section 7/8, leaving cheap room now), not dead code; it's an open decision whether to trim them at some point.

### Known gaps (not blocking, pending a future flow)

1. Nothing creates rows in `home.reminder` yet — there's no flow/command for the user to schedule a reminder.
2. `orchestrator` doesn't subscribe to `home/events/{reminder,price_alert,firmware_update}` yet — the "reminder needs Claude to word it" path publishes the event, but nothing consumes it today.
3. Nothing creates rows in `home.app_user` — channel/user resolution in `notifier-scheduler` won't find anything until that flow exists.
4. Verify before going to production: the exact name/version of the `web_search_20250305` tool in `shared/claude.py`, against the current `anthropic` SDK docs.
5. The parameterized `Dockerfile.service` hasn't been tested with a real Docker build in this session (no Docker available in this environment) — validated only by careful reading of the syntax and the compose variables.

**Pending (functional, suggested order):** the reminder-creation flow (closes gaps 1-3) → verification against real infrastructure (broker/Postgres/PostgREST/Claude on an actual node).

**Blockers/open decisions:** none.
