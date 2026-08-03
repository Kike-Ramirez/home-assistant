# shared

Common library used by all five services. Not a deployable service on its own — it's a `uv` workspace package the others depend on (`shared = { workspace = true }` in each service's `pyproject.toml`).

## What's in here

| Module | Purpose |
|---|---|
| `message.py` | The normalized MQTT message contract every channel adapter speaks (`NormalizedMessage`), topic naming helpers (`inbound_topic`, `outbound_topic`), and the internal event schemas used between `orchestrator` and `doc-ingestion-worker` (`DocIngestionRequest`/`DocIngestionResult`). |
| `mqtt_client.py` | Thin wrapper over [`aiomqtt`](https://github.com/empicano/aiomqtt): a connection context manager, `maintain_mqtt_connection()` (reconnect-forever loop with configurable backoff), and `ManagedMqttConnection` (keeps the latest live connection around so a channel adapter can publish from outside its own consume loop). |
| `postgrest_client.py` | Thin wrapper over [`postgrest-py`](https://github.com/supabase/postgrest-py) (the official PostgREST client). Keeps a stable `select`/`insert`/`upsert_on_conflict`/`patch`/`rpc` interface so call sites don't need to know the underlying query-builder syntax. |
| `settings.py` | Everything about service configuration: the secrets/appconfig split, hot-reloading `appconfig.json`, and the "never crash on a config/connection problem" retry policy. See below — this is the one module worth actually reading before touching any service's `config.py`. |
| `claude.py` | `call_structured()` — forces Claude to return a Pydantic-validated object via `tool_choice`, retrying if the response doesn't match the schema. Used by every "what does the user actually mean" decision across the codebase, plus the vision extraction in `doc-ingestion-worker`. |

## The configuration model

Every service follows the same pattern (this is what `bootstrap_service()` sets up in one call):

- **Secrets** — environment variables, loaded once at process start via `pydantic-settings`. If a required one is missing, the service does **not** crash: it logs an `ERROR` naming the exact variable and keeps retrying on a loop (using `connectTimeoutMs` as the interval) until it's there. A secrets change always needs a restart to take effect — that's just how env vars work.
- **AppConfig** — `appconfig.json`, matching the shape Barbara's own connectors use:
  ```json
  { "<service_name>": { "system": { "debugLevel": "info", "connectTimeoutMs": 15000 }, "anyOtherParam": "..." } }
  ```
  `system.debugLevel` sets the minimum log level (`debug`/`info`/`warn`/`error`). `system.connectTimeoutMs` sets the wait between reconnect attempts for anything that can drop a connection (MQTT, mostly). Anything else is a sibling key of `system`, specific to that service.

  **This file hot-reloads.** A background task rereads it every 10 seconds, logs whatever changed (old value → new), and applies it without a restart — including the log level and reconnect timeout. The one real exception is `web-adapter`'s `port`: the HTTP server has already bound the socket by the time it could notice a change, so that one still needs a restart.

- **Never crashes on a connection problem.** MQTT reconnects forever with backoff (`maintain_mqtt_connection`); a missing secret retries forever instead of exiting. In the channel adapters, the MQTT connection lives in its own background task, independent of the channel itself — so Telegram polling (or the web server) keeps working even while MQTT is down.

## Dependencies

`pydantic`, `pydantic-settings`, `aiomqtt`, `postgrest`, `anthropic` — see `pyproject.toml`. `postgrest-py` pulls in `httpx` on its own, so we don't declare it separately.
