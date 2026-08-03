from __future__ import annotations

from shared.settings import (
    DocIngestionWorkerSecrets,
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
# orchestrator is the one calling OUT to doc-ingestion-worker's /extract — it's
# the only service left that needs to know where to find it.
doc_ingestion_worker_secrets = load_secrets(DocIngestionWorkerSecrets, SERVICE_NAME, system.connect_timeout_seconds)
