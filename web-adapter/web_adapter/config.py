from __future__ import annotations

from shared.settings import MqttSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "web_adapter"

system, appconfig = bootstrap_service(SERVICE_NAME)

mqtt_secrets = load_secrets(MqttSecrets, SERVICE_NAME, system.connect_timeout_seconds)
