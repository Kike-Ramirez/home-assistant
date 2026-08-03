"""Anthropic Claude implementation of the `Engine` protocol (`base.py`).

This is the original agent-loop code from this project's Claude-only phase,
moved here unchanged in substance — only wrapped into a class so it can sit
behind `get_engine()` alongside other providers (e.g. Gemini). Manual
tool-use loop, not the SDK's beta Tool Runner — no beta dependency, and full
control over pausing mid-loop for an async tool (see `async_tool_names`).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from ..message import Attachment, AttachmentKind
from .base import DEFAULT_MAX_ITERATIONS_FALLBACK, AgentTurnResult, ModelT, ToolExecutor

if TYPE_CHECKING:
    pass

_WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}
_WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch"}


def _parse_data_uri(url_or_data: str, media_type: str | None, default_media_type: str) -> tuple[str, str]:
    header, _, data = url_or_data.partition(",")
    resolved_media_type = media_type or header.removeprefix("data:").split(";")[0] or default_media_type
    return resolved_media_type, data


def _image_block(url_or_data: str, media_type: str | None = None) -> dict[str, Any]:
    if url_or_data.startswith("data:"):
        resolved_media_type, data = _parse_data_uri(url_or_data, media_type, "image/jpeg")
        return {"type": "image", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url_or_data}}


async def _document_block(url_or_data: str, media_type: str | None, http_client: httpx.AsyncClient) -> dict[str, Any]:
    if url_or_data.startswith("data:"):
        resolved_media_type, data = _parse_data_uri(url_or_data, media_type, "application/pdf")
        return {"type": "document", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}
    response = await http_client.get(url_or_data)
    response.raise_for_status()
    data = base64.standard_b64encode(response.content).decode("ascii")
    resolved_media_type = media_type or response.headers.get("content-type", "application/pdf").split(";")[0]
    return {"type": "document", "source": {"type": "base64", "media_type": resolved_media_type, "data": data}}


async def _attachment_blocks(attachments: list[Attachment], http_client: httpx.AsyncClient | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    client = http_client
    owns_client = False
    try:
        for attachment in attachments:
            if attachment.kind == AttachmentKind.IMAGE:
                blocks.append(_image_block(attachment.url_or_data, attachment.media_type))
            elif attachment.kind == AttachmentKind.DOCUMENT:
                if client is None:
                    client = httpx.AsyncClient()
                    owns_client = True
                blocks.append(await _document_block(attachment.url_or_data, attachment.media_type, client))
            else:  # AUDIO — no Claude content-block type exists for it yet
                blocks.append(
                    {"type": "text", "text": f"[Adjunto de audio recibido — aún no soportado: {attachment.filename or attachment.url_or_data}]"}
                )
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return blocks


def _normalize_tool_use(block: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(block, dict):
        return block["id"], block["name"], block["input"]
    return block.id, block.name, block.input


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    return json.dumps(result)


async def _run_one_tool(executor: ToolExecutor, tool_use_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await executor(name, tool_input)
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": _stringify_tool_result(result)}
    except Exception as exc:  # noqa: BLE001 — any tool failure must come back to the model, not crash the loop
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": str(exc), "is_error": True}


async def _execute_tool_batch(executor: ToolExecutor, blocks: list[Any]) -> dict[str, Any]:
    normalized = [_normalize_tool_use(b) for b in blocks]
    results = await asyncio.gather(*(_run_one_tool(executor, tid, name, inp) for tid, name, inp in normalized))
    # All results go back in ONE user message — per Anthropic's own guidance,
    # splitting parallel tool_results across multiple messages measurably
    # trains the model to stop making parallel calls.
    return {"role": "user", "content": results}


def _with_cache_breakpoint(working: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of `working` with a cache_control breakpoint on the last content
    block of the last message — never mutates the persisted history itself.
    Combined with the system-prompt breakpoint, this covers 2 of the max 4
    breakpoints per request."""
    if not working:
        return working
    *head, last = working
    content = last.get("content")
    if isinstance(content, list) and content:
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
        last = {**last, "content": new_content}
    return [*head, last]


def extract_text(response: Any) -> str:
    return "\n".join(block.text for block in response.content if block.type == "text")


class AnthropicEngine:
    def __init__(self, api_key: str, model_name: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model_name = model_name

    async def build_user_message(
        self, text: str | None, attachments: list[Attachment], http_client: httpx.AsyncClient | None = None
    ) -> dict[str, Any]:
        blocks = await _attachment_blocks(attachments, http_client)
        if text:
            blocks.append({"type": "text", "text": text})
        return {"role": "user", "content": blocks}

    def find_tool_use(self, messages: list[dict[str, Any]], name: str | None = None) -> dict[str, Any] | None:
        if not messages:
            return None
        content = messages[-1].get("content")
        if not isinstance(content, list):
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and (name is None or block.get("name") == name):
                return block
        return None

    async def _loop(
        self,
        system: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        working: list[dict[str, Any]],
        max_tokens: int,
        max_iterations: int,
        async_tool_names: frozenset[str],
        max_iterations_fallback: str,
        web_search: bool,
    ) -> AgentTurnResult:
        api_tools = [*tools, _WEB_SEARCH_TOOL, _WEB_FETCH_TOOL] if web_search else tools
        for _ in range(max_iterations):
            response = await self._client.messages.create(
                model=self._model_name,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=api_tools,
                messages=_with_cache_breakpoint(working),
            )
            working = [*working, {"role": "assistant", "content": [block.model_dump() for block in response.content]}]

            if response.stop_reason != "tool_use":
                return AgentTurnResult(done=True, final_text=extract_text(response), messages=working)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if any(b.name in async_tool_names for b in tool_uses):
                # Don't execute ANY tool in this batch — including the ones
                # that aren't async — so a side-effecting call (e.g.
                # create_device) never runs only to have its result discarded
                # when the batch turns out to need pausing.
                return AgentTurnResult(done=False, final_text=extract_text(response) or None, messages=working)

            working = [*working, await _execute_tool_batch(tool_executor, tool_uses)]

        return AgentTurnResult(done=True, final_text=max_iterations_fallback, messages=working)

    async def run_agent_loop(
        self,
        system: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 4096,
        max_iterations: int = 6,
        async_tool_names: frozenset[str] = frozenset(),
        max_iterations_fallback: str = DEFAULT_MAX_ITERATIONS_FALLBACK,
        web_search: bool = True,
    ) -> AgentTurnResult:
        return await self._loop(
            system, tools, tool_executor, list(messages), max_tokens, max_iterations, async_tool_names, max_iterations_fallback, web_search
        )

    async def resume_agent_loop(
        self,
        system: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        messages: list[dict[str, Any]],
        resolved_tool_results: dict[str, Any],
        *,
        max_tokens: int = 4096,
        max_iterations: int = 6,
        async_tool_names: frozenset[str] = frozenset(),
        max_iterations_fallback: str = DEFAULT_MAX_ITERATIONS_FALLBACK,
        web_search: bool = True,
    ) -> AgentTurnResult:
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
        return await self._loop(
            system, tools, tool_executor, working, max_tokens, max_iterations, async_tool_names, max_iterations_fallback, web_search
        )

    async def call_structured(
        self,
        system: str,
        text: str | None,
        attachments: list[Attachment],
        tool_name: str,
        model: type[ModelT],
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> ModelT:
        user_content = await _attachment_blocks(attachments, None)
        if text:
            user_content.append({"type": "text", "text": text})

        schema = model.model_json_schema()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        tools = [{"name": tool_name, "description": f"Returns the result of: {system}", "input_schema": schema}]

        last_error: Exception | None = None
        for _ in range(max_retries + 1):
            response = await self._client.messages.create(
                model=self._model_name,
                max_tokens=max_tokens,
                system=system,
                tools=tools,
                tool_choice={"type": "tool", "name": tool_name},
                messages=messages,
            )
            tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == tool_name), None)
            if tool_use is None:
                raise RuntimeError(f"Model didn't return a structured response for '{tool_name}'")
            try:
                return model.model_validate(tool_use.input)
            except ValidationError as exc:
                last_error = exc
                messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": f"That response doesn't match the expected schema ({exc}). Fix it."},
                ]

        raise RuntimeError(f"Model didn't return a valid '{tool_name}' after {max_retries + 1} attempts: {last_error}")
