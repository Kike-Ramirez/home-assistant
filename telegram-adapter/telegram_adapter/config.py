from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.settings import bootstrap_service, load_secrets

SERVICE_NAME = "telegram_adapter"

system, appconfig, mqtt_secrets = bootstrap_service(SERVICE_NAME)


class TelegramSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", extra="ignore")

    bot_token: str


telegram_secrets = load_secrets(TelegramSecrets, SERVICE_NAME, system.connect_timeout_seconds)
