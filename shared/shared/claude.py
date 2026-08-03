"""Shared helper for forcing Claude to return a validated object.

Uses `tool_choice` (forces a tool call) + a Pydantic model (derives the
schema and validates the response, retrying if it doesn't match) — same
practical result a library like `instructor` would give you, without adding
it (it pulls in `openai` as a hard dependency and pins `anthropic` to an
exact version — more than we need here).

Used by both `orchestrator` (intent classification, confirmation/quiz-answer
interpretation, course generation) and `doc-ingestion-worker` (extracting
data from a photo).

Async (`AsyncAnthropic`), not the sync client: now that `orchestrator` is the
single point of contact for the Claude API, a blocking call here would stall
every other conversation in the house for however long that one call takes —
that's a much bigger blast radius than when Claude calls were split across
two separate processes.
"""

from __future__ import annotations

from typing import Any, TypeVar

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


async def call_structured(
    client: AsyncAnthropic,
    model_name: str,
    system: str,
    user_content: str | list[dict[str, Any]],
    tool_name: str,
    model: type[ModelT],
    max_tokens: int = 1024,
    max_retries: int = 2,
) -> ModelT:
    """Forces Claude (via tool_choice) to return an object validated against `model`.

    If the response doesn't validate against the schema, it's asked to fix it
    (up to `max_retries` times) instead of failing on the first mismatch.
    `user_content` can be plain text or a list of content blocks (e.g. image +
    text), same as Claude's own API accepts.
    """
    schema = model.model_json_schema()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools = [{"name": tool_name, "description": f"Returns the result of: {system}", "input_schema": schema}]

    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        response = await client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )
        tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == tool_name), None)
        if tool_use is None:
            raise RuntimeError(f"Claude didn't return a structured response for '{tool_name}'")
        try:
            return model.model_validate(tool_use.input)
        except ValidationError as exc:
            last_error = exc
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"That response doesn't match the expected schema ({exc}). Fix it."},
            ]

    raise RuntimeError(f"Claude didn't return a valid '{tool_name}' after {max_retries + 1} attempts: {last_error}")


def extract_text(response: Any) -> str:
    return "\n".join(block.text for block in response.content if block.type == "text")
