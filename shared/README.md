# shared

Common library used by all five services. Not a deployable service on its own — it's a `uv` workspace package the others depend on (`shared = { workspace = true }` in each service's `pyproject.toml`).

## What's in here

| Module | Purpose |
|---|---|
| `message.py` | The normalized MQTT message contract every channel adapter speaks (`NormalizedMessage`), topic naming helpers (`inbound_topic`, `outbound_topic`), and the JSON body schemas used between `orchestrator` and `doc-ingestion-worker` over HTTP (`DocIngestionRequest`/`DocIngestionResult`) — these used to be MQTT event payloads, now they're internal API request/response bodies. |
| `mqtt_client.py` | Thin wrapper over [`aiomqtt`](https://github.com/empicano/aiomqtt): a connection context manager, `maintain_mqtt_connection()` (reconnect-forever loop with configurable backoff), and `ManagedMqttConnection` (keeps the latest live connection around so a service can publish from outside its own consume loop — used by the channel adapters and by `orchestrator`, the only services with an MQTT connection). |
| `postgrest_client.py` | Thin wrapper over [`postgrest-py`](https://github.com/supabase/postgrest-py) (the official PostgREST client). Keeps a stable `select`/`insert`/`upsert_on_conflict`/`patch`/`rpc` interface so call sites don't need to know the underlying query-builder syntax. Only `orchestrator` uses this now. |
| `internal_client.py` | `InternalApiClient` — a thin `httpx`-based client for calling another service's internal HTTP API (used by `doc-ingestion-worker` and `notifier-scheduler` to reach `orchestrator`). Bounded retry (a couple of attempts with a short delay), not retry-forever like the MQTT reconnect loop — these are one-off request/response calls tied to something happening right now, not a long-lived connection worth resurrecting indefinitely. Returns `None` (after logging) on total failure instead of raising, so callers can degrade gracefully. |
| `settings.py` | Everything about service configuration: the secrets/appconfig split (both shared, repo-wide files — see below), hot-reloading `appconfig.json`, and the "never crash on a config/connection problem" retry policy. See below — this is the one module worth actually reading before touching any service's `config.py`. |
| `claude.py` | `call_structured()` — forces Claude to return a Pydantic-validated object via `tool_choice`, retrying if the response doesn't match the schema. **Async** (`AsyncAnthropic`, not the sync `Anthropic` client): now that all conversational Claude calls are centralized in `orchestrator`, a blocking synchronous call would stall every conversation in the house, not just one process's worth of work. Used by every "what does the user actually mean" decision across the codebase, plus the vision extraction in `doc-ingestion-worker` (which keeps its own `AsyncAnthropic` client — see that service's README for why). |

## The configuration model

This matches Barbara's own boilerplate exactly (see [`boilerplate_01_python`](https://github.com/Barbaraedge/training_barbara_apps_development/tree/main/boilerplate_01_python)), rather than a per-service scheme invented for this project — **one appconfig, one secrets file, for the whole app**, not one per service. Every service follows the same pattern (this is what `bootstrap_service()` sets up in one call):

- **Secrets** — one file, `barbarasecrets.env` at the repo root, injected identically as environment variables into every container (`env_file:` in `docker-compose-local.yml`; the platform does the equivalent on a real Barbara node). Loaded once at process start via `pydantic-settings`, and each service's `config.py` only reads the specific variables it needs (e.g. `MqttSecrets` reads `MQTT_*`) — a variable meant for another service is just ignored. If a required one is missing, the service does **not** crash: it logs an `ERROR` naming the exact variable and keeps retrying on a loop (using `connectTimeoutMs` as the interval) until it's there. A secrets change always needs a restart to take effect — that's just how env vars work.
- **AppConfig (application level)** — one file, `appconfigDev/appconfig.json` at the repo root in dev (mounted read-only at `/appconfig/appconfig.json` in every container), one top-level key per service, matching the shape Barbara's own connectors use:
  ```json
  { "<service_name>": { "system": { "debugLevel": "info", "connectTimeoutMs": 15000 }, "anyOtherParam": "..." } }
  ```
  `system.debugLevel` sets the minimum log level (`debug`/`info`/`warn`/`error`). `system.connectTimeoutMs` sets the wait between reconnect attempts for anything that can drop a connection (MQTT, mostly). Anything else is a sibling key of `system`, specific to that service. `load_service_config()`/`bootstrap_service()` pick out just the caller's own top-level key — a service never sees another service's settings.

  **This file hot-reloads.** A background task (`watch_appconfig`) rereads it every 10 seconds, logs whatever changed (old value → new), and applies it without a restart — including the log level and reconnect timeout. The one real exception is a service's own `port` (`web-adapter`, `orchestrator`, `doc-ingestion-worker`): the HTTP server has already bound the socket by the time it could notice a change, so those still need a restart.

- **AppConfig (device level)** — `appconfigDev/global.json`, mounted at `/appconfig/global.json`. Barbara's device-wide config layer, readable via `load_global_config()` — not consumed by any service in this project yet, included so it's there the moment something needs it.

- **Never crashes on a connection problem.** MQTT reconnects forever with backoff (`maintain_mqtt_connection`); a missing secret retries forever instead of exiting. In the channel adapters, the MQTT connection lives in its own background task, independent of the channel itself — so Telegram polling (or the web server) keeps working even while MQTT is down.

## Dependencies

`pydantic`, `pydantic-settings`, `aiomqtt`, `postgrest`, `anthropic`, `httpx` — see `pyproject.toml`. `httpx` is declared directly (used by `internal_client.py`), even though `postgrest-py` also pulls it in on its own.
