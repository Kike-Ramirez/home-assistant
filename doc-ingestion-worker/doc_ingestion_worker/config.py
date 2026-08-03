from __future__ import annotations

from shared.settings import AnthropicSecrets, OrchestratorSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "doc_ingestion_worker"

system, appconfig = bootstrap_service(SERVICE_NAME)

# No MQTT anymore — orchestrator calls this service's /extract directly, and
# this service calls back to orchestrator's internal API when done. Keeps its
# own Claude client though (see orchestrator/README.md for why that one
# specific connection stays here instead of also being centralized).
anthropic_secrets = load_secrets(AnthropicSecrets, SERVICE_NAME, system.connect_timeout_seconds)
orchestrator_secrets = load_secrets(OrchestratorSecrets, SERVICE_NAME, system.connect_timeout_seconds)
