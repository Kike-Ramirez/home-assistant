"""Two Claude helpers: a forced-single-tool call for structured extraction,
and a full multi-turn tool-use loop for the orchestrator's agent.

`call_structured` uses `tool_choice` (forces a tool call) + a Pydantic model
(derives the schema and validates the response, retrying if it doesn't
match) — same practical result a library like `instructor` would give you,
without adding it (it pulls in `openai` as a hard dependency and pins
`anthropic` to an exact version — more than we need here). Used by
`doc-ingestion-worker` for vision extraction.

`run_agent_loop`/`resume_agent_loop` are the orchestrator's actual
intelligence: Claude gets the full conversation plus a set of tools, and
decides everything itself — which tool(s) to call, whether to call any at
all, what to say. Orchestrator never branches on intent; it just executes
whatever Claude asks for (CLAUDE.md section 10, "Claude drives via tool-use").
Manual loop, not the SDK's beta Tool Runner — same reasoning as skipping
`instructor`, plus this needs one thing the Tool Runner doesn't expose: the
ability to pause mid-loop when a tool kicks off async external work (see
`async_tool_names` below) and resume later once that work completes.

Async (`AsyncAnthropic`), not the sync client: now that `orchestrator` is the
single point of contact for the Claude API, a blocking call here would stall
every other conversation in the house for however long that one call takes —
that's a much bigger blast radius than when Claude calls were split across
two separate processes.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from .message import Attachment, AttachmentKind

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


# =============================================================================
# Agent loop — Claude decides, orchestrator only executes
# =============================================================================

_DEFAULT_MAX_ITERATIONS_FALLBACK = "I got stuck in a loop on this one — could you rephrase what you need, or give me more detail?"
# Generic English default — `shared` is domain-agnostic infra, so the
# household-specific (Spanish) wording lives in orchestrator/claude_client.py
# and gets passed in via `max_iterations_fallback` instead of being hardcoded here.

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class AgentTurnResult:
    """Outcome of one `run_agent_loop`/`resume_agent_loop` call.

    When `done` is False, `messages` ends with the assistant turn that
    requested an async tool (one whose name is in `async_tool_names`) —
    the caller is expected to persist `messages` verbatim and later call
    `resume_agent_loop` with that same list plus the resolved result.
    `final_text` in that case is whatever text Claude wrote before making
    the tool call (often present — models routinely say a sentence before
    reaching for a tool), used as a provisional reply; it can be empty.
    """

    done: bool
    final_text: str | None
    messages: list[dict[str, Any]]


def _normalize_tool_use(block: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(block, dict):
        return block["id"], block["name"], block["input"]
    return block.id, block.name, block.input


def find_tool_use(messages: list[dict[str, Any]], name: str | None = None) -> dict[str, Any] | None:
    """Finds a `tool_use` block in the last message of `messages` — the
    assistant turn `run_agent_loop` paused on, once it's been persisted and
    reloaded as plain dicts. Callers that need to know which tool call is
    pending (e.g. to fire the actual async work, or to build
    `resolved_tool_results` for `resume_agent_loop`) use this instead of
    reaching into the raw content-block shape themselves. Pass `name` to
    find a specific tool; omit it to get the first `tool_use` block at all.
    """
    if not messages:
        return None
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and (name is None or block.get("name") == name):
            return block
    return None


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    import json

    return json.dumps(result)


async def _run_one_tool(executor: ToolExecutor, tool_use_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await executor(name, tool_input)
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": _stringify_tool_result(result)}
    except Exception as exc:  # noqa: BLE001 — any tool failure must come back to Claude, not crash the loop
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(exc), "is_error": True}


async def _execute_tool_batch(executor: ToolExecutor, blocks: list[Any]) -> dict[str, Any]:
    normalized = [_normalize_tool_use(b) for b in blocks]
    results = await asyncio.gather(*(_run_one_tool(executor, tid, name, inp) for tid, name, inp in normalized))
    # All results go back in ONE user message — per Anthropic's own guidance,
    # splitting parallel tool_results across multiple messages measurably
    # trains Claude to stop making parallel calls.
    return {"role": "user", "content": results}


def _with_cache_breakpoint(working: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of `working` with a cache_control breakpoint on the last content
    block of the last message — never mutates the persisted history itself.
    Combined with the breakpoint on the system prompt (in `_loop`), this
    covers 2 of the max 4 breakpoints per request. Render order is
    tools -> system -> messages, so the system breakpoint alone already
    caches the (static) tool schema list too; this second breakpoint is what
    lets every earlier turn's cache entry keep being read as history grows.
    """
    if not working:
        return working
    *head, last = working
    content = last.get("content")
    if isinstance(content, list) and content:
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
        last = {**last, "content": new_content}
    return [*head, last]


async def _loop(
    client: AsyncAnthropic,
    model_name: str,
    system: str,
    tools: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    working: list[dict[str, Any]],
    max_tokens: int,
    max_iterations: int,
    async_tool_names: frozenset[str],
    max_iterations_fallback: str,
) -> AgentTurnResult:
    for _ in range(max_iterations):
        response = await client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=_with_cache_breakpoint(working),
        )
        working = [*working, {"role": "assistant", "content": [block.model_dump() for block in response.content]}]

        if response.stop_reason != "tool_use":
            return AgentTurnResult(done=True, final_text=extract_text(response), messages=working)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if any(b.name in async_tool_names for b in tool_uses):
            # Don't execute ANY tool in this batch — including the ones that
            # aren't async — so a side-effecting call (e.g. create_device)
            # never runs only to have its result discarded when the batch
            # turns out to need pausing. The whole batch resumes together
            # once the async result is known (see resume_agent_loop).
            return AgentTurnResult(done=False, final_text=extract_text(response) or None, messages=working)

        working = [*working, await _execute_tool_batch(tool_executor, tool_uses)]

    return AgentTurnResult(done=True, final_text=max_iterations_fallback, messages=working)


async def run_agent_loop(
    client: AsyncAnthropic,
    model_name: str,
    system: str,
    tools: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    messages: list[dict[str, Any]],
    max_tokens: int = 4096,
    max_iterations: int = 6,
    async_tool_names: frozenset[str] = frozenset(),
    max_iterations_fallback: str = _DEFAULT_MAX_ITERATIONS_FALLBACK,
) -> AgentTurnResult:
    """Runs Claude to `stop_reason == "end_turn"` (or `max_iterations`),
    executing whatever tools it calls along the way. `messages` is the
    conversation so far (the new user turn already appended) — the full
    updated list comes back on `AgentTurnResult.messages` for the caller to
    persist, whether the turn finished or paused.
    """
    return await _loop(
        client, model_name, system, tools, tool_executor, list(messages), max_tokens, max_iterations, async_tool_names, max_iterations_fallback
    )


async def resume_agent_loop(
    client: AsyncAnthropic,
    model_name: str,
    system: str,
    tools: list[dict[str, Any]],
    tool_executor: ToolExecutor,
    messages: list[dict[str, Any]],
    resolved_tool_results: dict[str, Any],
    max_tokens: int = 4096,
    max_iterations: int = 6,
    async_tool_names: frozenset[str] = frozenset(),
    max_iterations_fallback: str = _DEFAULT_MAX_ITERATIONS_FALLBACK,
) -> AgentTurnResult:
    """Continues a turn `run_agent_loop` paused. `messages` must be exactly
    what that call returned (ending with the assistant turn that requested
    the async tool). `resolved_tool_results` maps `tool_use_id` (each one is
    already unique per call, so this works even if a batch ever contains more
    than one call to the same async tool) to the now-known result for
    whichever tool(s) triggered the pause; any other tool in that same batch
    is executed for real now, same as usual.
    """
    working = list(messages)
    pending_tool_uses = [b for b in working[-1]["content"] if b.get("type") == "tool_use"]

    async def _run(block: dict[str, Any]) -> dict[str, Any]:
        tool_use_id, name, tool_input = _normalize_tool_use(block)
        if tool_use_id in resolved_tool_results:
            content = _stringify_tool_result(resolved_tool_results[tool_use_id])
            return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
        return await _run_one_tool(tool_executor, tool_use_id, name, tool_input)

    results = await asyncio.gather(*(_run(b) for b in pending_tool_uses))
    working = [*working, {"role": "user", "content": results}]
    return await _loop(
        client, model_name, system, tools, tool_executor, working, max_tokens, max_iterations, async_tool_names, max_iterations_fallback
    )


# =============================================================================
# Content blocks — turning an `Attachment` into what Claude's API accepts
# =============================================================================


def _parse_data_uri(url_or_data: str, media_type: str | None, default_media_type: str) -> tuple[str, str]:
    """Shared by `image_block`/`_document_block`: splits a `data:<mime>;base64,<data>`
    URI into (media_type, base64 data), falling back to the URI's own declared
    mime type, then `default_media_type`, if the caller didn't supply one."""
    header, _, data = url_or_data.partition(",")
    resolved_media_type = media_type or header.removeprefix("data:").split(";")[0] or default_media_type
    return resolved_media_type, data


def image_block(url_or_data: str, media_type: str | None = None) -> dict[str, Any]:
    """Used for both a raw attachment URL/base64 string (doc-ingestion-worker's
    vision extraction) and `Attachment`-based messages (`build_content_blocks`
    below) — image blocks accept a `url` source directly, so a public URL
    (e.g. Telegram's `api.telegram.org/file/...`) never needs downloading."""
    if url_or_data.startswith("data:"):
        resolved_media_type, data = _parse_data_uri(url_or_data, media_type, "image/jpeg")
        return {"type": "image", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url_or_data}}


async def _document_block(url_or_data: str, media_type: str | None, http_client: httpx.AsyncClient) -> dict[str, Any]:
    """Unlike `image`, Claude's `document` content block has no `url` source
    type — only `base64` or a Files API `file_id`. A document attachment
    that's still a public URL (e.g. a Telegram document) has to be
    downloaded and base64-encoded here before it can be sent."""
    if url_or_data.startswith("data:"):
        resolved_media_type, data = _parse_data_uri(url_or_data, media_type, "application/pdf")
        return {"type": "document", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}
    response = await http_client.get(url_or_data)
    response.raise_for_status()
    data = base64.standard_b64encode(response.content).decode("ascii")
    resolved_media_type = media_type or response.headers.get("content-type", "application/pdf").split(";")[0]
    return {"type": "document", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}


async def build_content_blocks(
    text: str | None,
    attachments: list[Attachment],
    http_client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Builds one user turn's content blocks: attachments first (matching
    Anthropic's own vision examples — the image/document precedes the text
    that refers to it), then the text itself, if any.

    `http_client` is only used for a `document`-kind attachment that arrives
    as a URL (see `_document_block`); when omitted, a throwaway client is
    created lazily — only if a document attachment that actually needs it
    shows up — since most messages are text/image-only and would otherwise
    pay for a client they never touch.
    """
    blocks: list[dict[str, Any]] = []
    client = http_client
    owns_client = False
    try:
        for attachment in attachments:
            if attachment.kind == AttachmentKind.IMAGE:
                blocks.append(image_block(attachment.url_or_data, attachment.media_type))
            elif attachment.kind == AttachmentKind.DOCUMENT:
                if client is None:
                    client = httpx.AsyncClient()
                    owns_client = True
                blocks.append(await _document_block(attachment.url_or_data, attachment.media_type, client))
            else:  # AUDIO — no Claude content-block type exists for it yet (see AttachmentKind)
                blocks.append(
                    {"type": "text", "text": f"[Adjunto de audio recibido — aún no soportado: {attachment.filename or attachment.url_or_data}]"}
                )
    finally:
        if owns_client and client is not None:
            await client.aclose()

    if text:
        blocks.append({"type": "text", "text": text})
    return blocks
