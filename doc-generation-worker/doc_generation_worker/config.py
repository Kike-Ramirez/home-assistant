from __future__ import annotations

from shared.settings import OrchestratorSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "doc_generation_worker"

system, appconfig = bootstrap_service(SERVICE_NAME)

# No MQTT, no Postgres, no LLM client — this service only renders bytes the
# model already wrote the content for. orchestrator calls this service's
# /generate directly, and this service calls back to orchestrator's internal
# API when done.
orchestrator_secrets = load_secrets(OrchestratorSecrets, SERVICE_NAME, system.connect_timeout_seconds)
