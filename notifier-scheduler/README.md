# notifier-scheduler

Checks for due reminders (maintenance nudges, price alerts, firmware updates) and pushes them out through the right channel. Cron-like, via [`APScheduler`](https://apscheduler.readthedocs.io/).

## How it works

- Runs a periodic job (every `checkIntervalSeconds`) that queries `home.reminder` for anything with `status=pending` and `scheduled_at <= now`, via PostgREST.
- For each due reminder: if it already has a ready-to-send `payload.message` and the target user's channel can be resolved, it's sent straight to `home/outbound/<channel>/<user_id>` — no need to go through `orchestrator`. Otherwise, it publishes the raw event to `home/events/{reminder,price_alert,firmware_update}` (by `kind`) for something downstream to turn into a properly-worded message.
- Recurring reminders (`recurrence_rule`, a standard iCal RRULE) get rescheduled to their next occurrence via [`dateutil.rrule`](https://dateutil.readthedocs.io/); one-off reminders get marked `sent`.
- **Unlike every other service here, this one doesn't keep a persistent MQTT connection** — it only ever publishes, never subscribes, so each scheduled tick opens a fresh connection, does its thing, and closes it. Simpler than managing the lifecycle of a connection that would sit idle most of the time. If that connection attempt fails, it retries once after `connectTimeoutMs`, then gives up until the next tick.
- The `APScheduler` jobstore lives in Postgres (`SQLAlchemyJobStore`) so scheduled jobs survive a container restart — that's what `POSTGRES_DSN` is for below, separate from the PostgREST connection used for actual domain data.

### Known gaps (not this service's fault — nothing upstream feeds it yet)

- Nothing in this codebase creates rows in `home.reminder` yet — there's no "remind me to..." flow wired up on the `orchestrator` side. This service is ready to serve reminders the moment something starts creating them.
- `orchestrator` doesn't subscribe to `home/events/{reminder,price_alert,firmware_update}` yet, so the "needs Claude to word it nicely" path publishes an event that nothing currently consumes.
- Nothing creates rows in `home.app_user` yet, so channel/user resolution for a reminder will come up empty until that exists too.

## Configuration

### Secrets (`.env`)

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `POSTGREST_URL` | yes | Base URL of the PostgREST instance, e.g. `http://postgrest:3000` |
| `POSTGRES_DSN` | yes | Direct Postgres connection string for the APScheduler jobstore, e.g. `postgresql://app_service:password@postgresql:5432/home` |
| `MQTT_HOST` | yes | MQTT broker hostname |
| `MQTT_PORT` | no (default `8883`) | MQTT broker port |
| `MQTT_USER` | yes | MQTT username |
| `MQTT_PASSWORD` | yes | MQTT password |
| `MQTT_TLS_ENABLED` | no (default `true`) | Whether to use TLS for the MQTT connection |

### AppConfig (`appconfig.json`)

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
