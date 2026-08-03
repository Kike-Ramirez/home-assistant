from __future__ import annotations

from shared.settings import OrchestratorSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "doc_ingestion_worker"

system, appconfig = bootstrap_service(SERVICE_NAME)

# No MQTT anymore — orchestrator calls this service's /extract directly, and
# this service calls back to orchestrator's internal API when done. Keeps its
# own Gemini client though (see orchestrator/README.md for why that one
# specific connection stays here instead of also being centralized) — the
# GEMINI_API_KEY secret is loaded directly in extractor.py.
orchestrator_secrets = load_secrets(OrchestratorSecrets, SERVICE_NAME, system.connect_timeout_seconds)
