from __future__ import annotations

from shared.settings import bootstrap_service

SERVICE_NAME = "web_adapter"

system, appconfig, mqtt_secrets = bootstrap_service(SERVICE_NAME)
