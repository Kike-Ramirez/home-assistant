from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.settings import GeminiSecrets, OrchestratorSecrets, bootstrap_service, load_secrets

SERVICE_NAME = "image_generation_worker"

system, appconfig = bootstrap_service(SERVICE_NAME)

# No MQTT, no Postgres — reached via orchestrator's internal API, calls back
# the same way (same shape as doc-ingestion-worker/doc-generation-worker).
orchestrator_secrets = load_secrets(OrchestratorSecrets, SERVICE_NAME, system.connect_timeout_seconds)
# For the Gemini image-generation fallback (generate.py) — required, since
# every request needs SOME way to produce an image even when search finds
# nothing usable.
gemini_secrets = load_secrets(GeminiSecrets, SERVICE_NAME, system.connect_timeout_seconds)


class GoogleImageSearchSecrets(BaseSettings):
    """Google Custom Search JSON API (a Programmable Search Engine configured
    for image search) — the "find a real photo" half of the `generate_image`
    tool. Optional, unlike `gemini_secrets` above: with either field unset,
    `search.py::search_image` just returns `None` immediately and the caller
    falls straight to the Gemini fallback, so this never blocks startup the
    way a real required secret would (`load_secrets` isn't used here)."""

    model_config = SettingsConfigDict(env_prefix="GOOGLE_CSE_", extra="ignore")

    api_key: str = ""
    cx: str = ""


image_search_secrets = GoogleImageSearchSecrets()
