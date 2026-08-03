# notifier-scheduler

A cron heartbeat, nothing more. Used to check for due reminders (maintenance nudges, price alerts, firmware updates) and push them out through the right channel itself, talking to PostgREST and MQTT directly. Under the "`orchestrator` owns every external/shared connection" design (see the root [README's design notes](../README.md#design-notes)), all of that logic moved to `orchestrator` (`orchestrator/orchestrator/reminders.py`) — this service's only remaining job is to ping `orchestrator` on a timer via [`APScheduler`](https://apscheduler.readthedocs.io/).

It still exists as its own service (rather than just being an `APScheduler` job inside `orchestrator`) because of the one thing it still owns: its own Postgres-backed jobstore, so scheduled ticks survive a container restart independently of `orchestrator`'s own process lifecycle.

## How it works

- Runs a periodic job (every `checkIntervalSeconds`) that calls `POST /internal/reminders/check` on `orchestrator` — no PostgREST, no MQTT, no reminder logic of its own anymore.
- `orchestrator` does the actual work behind that endpoint: reads `home.reminder` for anything due, sends the ones with a ready-to-send message straight to the user's channel, asks Claude to word the ones that need it (closing what used to be a dead-end gap — nothing consumed that path before), reschedules recurring reminders via `dateutil.rrule`, and marks one-off ones `sent`. It replies `{"processed": <count>}`.
- If the call to `orchestrator` fails, `shared.internal_client.InternalApiClient` already retries a couple of times and logs the failure — this service doesn't need its own retry logic on top; the next scheduled tick will just try again.
- The `APScheduler` jobstore lives in Postgres (`SQLAlchemyJobStore`) so scheduled jobs survive a container restart — that's what `POSTGRES_DSN` is for below. This is now the only Postgres connection this service has (no more PostgREST access to domain data).

## Configuration

Like every service here, this reads its config from the **shared** files at the repo root — see the [root README](../README.md#configuration-one-shared-appconfig--one-shared-secrets-file) for why. Below are just the parts this service actually reads.

### Secrets (`barbarasecrets.env`)

Fill in these variables in the repo-root [`barbarasecrets.env`](../barbarasecrets.env):

| Variable | Required | Description |
|---|---|---|
| `ORCHESTRATOR_URL` | yes | Base URL of `orchestrator`'s internal API, e.g. `http://orchestrator:8080` |
| `POSTGRES_DSN` | yes | Direct Postgres connection string for the APScheduler jobstore, e.g. `postgresql://app_service:password@postgresql:5432/home` |

No `MQTT_*` or `POSTGREST_URL` needed anymore — see above, this service only ever talks to `orchestrator`.

### AppConfig (`appconfigDev/appconfig.json`)

`notifier-scheduler`'s own slice of the repo-root [`appconfigDev/appconfig.json`](../appconfigDev/appconfig.json):

```json
{
  "notifier_scheduler": {
    "system": {
      "debugLevel": "info",
      "connectTimeoutMs": 15000
    },
    "checkIntervalSeconds": 300
  }
}
```

| Key | Default | Description |
|---|---|---|
| `checkIntervalSeconds` | `300` | How often to check for due reminders |

Hot-reloadable — changing `checkIntervalSeconds` live reschedules the existing `APScheduler` job to the new interval instead of waiting for a restart.
