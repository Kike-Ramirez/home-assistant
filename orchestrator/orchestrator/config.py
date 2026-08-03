from __future__ import annotations

from shared.settings import AnthropicSecrets, PostgrestSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "orchestrator"

system, appconfig, mqtt_secrets = bootstrap_service(SERVICE_NAME)

postgrest_secrets = load_secrets(PostgrestSecrets, SERVICE_NAME, system.connect_timeout_seconds)
anthropic_secrets = load_secrets(AnthropicSecrets, SERVICE_NAME, system.connect_timeout_seconds)
