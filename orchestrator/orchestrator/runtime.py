"""Shared runtime objects for the agent loop — the one leaf module `main.py`,
`pauses.py`, and `callbacks.py` all import from, so none of the three needs
to import from either of the other two (which would risk a circular import,
since `main.py` calls into `pauses.py`, and `callbacks.py` calls into both
`runtime.py` and `pauses.py`).
"""

from __future__ import annotations

from typing import Any

from shared.gemini_client import AgentTurnResult
from shared.internal_client import InternalApiClient
from shared.mqtt_client import ManagedMqttConnection
from shared.postgrest_client import PostgrestClient

from . import actions, llm
from .config import (
    appconfig,
    doc_generation_worker_secrets,
    doc_ingestion_worker_secrets,
    image_generation_worker_secrets,
)

mqtt = ManagedMqttConnection("orchestrator")
doc_ingestion_client = InternalApiClient(doc_ingestion_worker_secrets.url, "orchestrator")
doc_generation_client = InternalApiClient(doc_generation_worker_secrets.url, "orchestrator")
image_generation_client = InternalApiClient(image_generation_worker_secrets.url, "orchestrator")

PAUSE_TOOL_NAMES = actions.ASYNC_TOOL_NAMES | actions.CONFIRM_TOOL_NAMES


def loop_kwargs() -> dict[str, Any]:
    """Shared config for every `run_agent_loop`/`resume_agent_loop` call — one
    spot to read live from appconfig instead of the two call sites drifting."""
    return {
        "max_tokens": llm.max_tokens(),
        "async_tool_names": PAUSE_TOOL_NAMES,
        "max_iterations_fallback": llm.MAX_ITERATIONS_FALLBACK,
        "web_search": appconfig.get("webSearchEnabled", True),
        "api_error_fallback": llm.API_ERROR_FALLBACK,
    }


async def run_agent_loop(pg: PostgrestClient, history: list[dict[str, Any]]) -> AgentTurnResult:
    return await llm.client.run_agent_loop(
        llm.SYSTEM_PROMPT, actions.TOOL_SCHEMAS, actions.make_executor(pg), history, **loop_kwargs()
    )


async def resume_agent_loop(
    pg: PostgrestClient, history: list[dict[str, Any]], resolved_tool_results: dict[str, Any]
) -> AgentTurnResult:
    return await llm.client.resume_agent_loop(
        llm.SYSTEM_PROMPT,
        actions.TOOL_SCHEMAS,
        actions.make_executor(pg),
        history,
        resolved_tool_results=resolved_tool_results,
        **loop_kwargs(),
    )


async def aclose_clients() -> None:
    await doc_ingestion_client.aclose()
    await doc_generation_client.aclose()
    await image_generation_client.aclose()
