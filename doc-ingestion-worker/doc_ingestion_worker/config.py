from __future__ import annotations

from shared.settings import AnthropicSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "doc_ingestion_worker"

system, appconfig, mqtt_secrets = bootstrap_service(SERVICE_NAME)

anthropic_secrets = load_secrets(AnthropicSecrets, SERVICE_NAME, system.connect_timeout_seconds)
