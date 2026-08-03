from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.settings import OrchestratorSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "notifier_scheduler"

system, appconfig = bootstrap_service(SERVICE_NAME)


class SchedulerSecrets(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Own DSN for the APScheduler jobstore (SQLAlchemyJobStore) — this is an
    # internal scheduler detail, not domain data access, so it doesn't go
    # through orchestrator/PostgREST (see CLAUDE.md section 10). Everything
    # else this service used to touch directly (PostgREST, MQTT) now goes
    # through orchestrator's internal API instead.
    postgres_dsn: str


scheduler_secrets = load_secrets(SchedulerSecrets, SERVICE_NAME, system.connect_timeout_seconds)
orchestrator_secrets = load_secrets(OrchestratorSecrets, SERVICE_NAME, system.connect_timeout_seconds)
