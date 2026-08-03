from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.settings import PostgrestSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "notifier_scheduler"

system, appconfig, mqtt_secrets = bootstrap_service(SERVICE_NAME)


class SchedulerSecrets(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Own DSN for the APScheduler jobstore (SQLAlchemyJobStore) — this is an
    # internal scheduler detail, not domain data access, so it doesn't go
    # through PostgREST (see CLAUDE.md section 10).
    postgres_dsn: str


postgrest_secrets = load_secrets(PostgrestSecrets, SERVICE_NAME, system.connect_timeout_seconds)
scheduler_secrets = load_secrets(SchedulerSecrets, SERVICE_NAME, system.connect_timeout_seconds)
