"""Factory for the pluggable LLM engine — the only place that maps an
`appconfig.json` `"engine"` value to a concrete `Engine` implementation and
loads that provider's own secret. Add a new provider by adding one branch
here (plus its `<name>_engine.py`) — nothing else in the codebase needs to
change.
"""

from __future__ import annotations

from ..settings import AnthropicSecrets, GeminiSecrets, load_secrets
from .base import AgentTurnResult, Engine, ToolExecutor

__all__ = ["AgentTurnResult", "Engine", "ToolExecutor", "get_engine"]


def get_engine(name: str, service_name: str, model_name: str, retry_seconds: float = 15.0) -> Engine:
    if name == "gemini":
        from .gemini_engine import GeminiEngine

        secrets = load_secrets(GeminiSecrets, service_name, retry_seconds)
        return GeminiEngine(secrets.api_key, model_name)
    if name == "anthropic":
        from .anthropic_engine import AnthropicEngine

        secrets = load_secrets(AnthropicSecrets, service_name, retry_seconds)
        return AnthropicEngine(secrets.api_key, model_name)
    raise ValueError(f"Unknown engine {name!r} — expected 'gemini' or 'anthropic'")
