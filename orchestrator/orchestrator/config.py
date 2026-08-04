from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.settings import (
    DocGenerationWorkerSecrets,
    DocIngestionWorkerSecrets,
    ImageGenerationWorkerSecrets,
    MqttSecrets,
    PostgrestSecrets,
    bootstrap_service,
    load_secrets,
)

SERVICE_NAME = "orchestrator"

system, appconfig = bootstrap_service(SERVICE_NAME)

mqtt_secrets = load_secrets(MqttSecrets, SERVICE_NAME, system.connect_timeout_seconds)
postgrest_secrets = load_secrets(PostgrestSecrets, SERVICE_NAME, system.connect_timeout_seconds)
# GEMINI_API_KEY is loaded directly in llm.py, not here.
# orchestrator is the one calling OUT to doc-ingestion-worker's /extract,
# doc-generation-worker's /generate, and image-generation-worker's
# /generate-image — it's the only service left that needs to know where to
# find any of them.
doc_ingestion_worker_secrets = load_secrets(DocIngestionWorkerSecrets, SERVICE_NAME, system.connect_timeout_seconds)
doc_generation_worker_secrets = load_secrets(DocGenerationWorkerSecrets, SERVICE_NAME, system.connect_timeout_seconds)
image_generation_worker_secrets = load_secrets(ImageGenerationWorkerSecrets, SERVICE_NAME, system.connect_timeout_seconds)


class WelcomeSecrets(BaseSettings):
    """The household's own Telegram chat id, so orchestrator can greet them
    proactively on startup — not a per-request secret, and optional: with it
    unset the welcome message is simply skipped (logged once), everything
    else works exactly the same."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="ignore")

    admin_chat_id: str = ""


# Not run through load_secrets (which retries forever on a missing value) —
# this one is optional by design, so a plain one-shot read is enough.
welcome_secrets = WelcomeSecrets()
