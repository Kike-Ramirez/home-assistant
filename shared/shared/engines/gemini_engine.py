"""Google Gemini implementation of the `Engine` protocol (`base.py`), using
`google-genai` (`from google import genai`).

Deliberate simplifications versus the Anthropic engine, accepted for a fast
first version (see CLAUDE.md / root README design notes) — revisit only if
they turn out to matter in practice:
- **No prompt caching.** Gemini's `cached_content` is an explicit, coarser
  mechanism (you create and manage a cache object yourself) — not wired up here.
- **Web search is `google_search` grounding**, not a `web_search`/`web_fetch`
  pair — there's no separate "fetch this specific URL" tool on this engine.
- **Every attachment is downloaded and sent as inline bytes.** Gemini has no
  `url` source type at all (unlike Claude's `image` blocks) — even a public
  image URL (e.g. Telegram's) has to be fetched here first.

Not yet verified against the live Gemini API (no key available while writing
this) — the shapes below come from introspecting the installed `google-genai`
package's type definitions, not from running requests end to end.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from ..message import Attachment, AttachmentKind
from .base import DEFAULT_MAX_ITERATIONS_FALLBACK, AgentTurnResult, ModelT, ToolExecutor


async def _attachment_part(attachment: Attachment, http_client: httpx.AsyncClient) -> dict[str, Any]:
    if attachment.kind == AttachmentKind.AUDIO:
        return {"text": f"[Adjunto de audio recibido — aún no soportado: {attachment.filename or attachment.url_or_data}]"}

    default_mime = "image/jpeg" if attachment.kind == AttachmentKind.IMAGE else "application/pdf"
    if attachment.url_or_data.startswith("data:"):
        header, _, b64_data = attachment.url_or_data.partition(",")
        media_type = attachment.media_type or header.removeprefix("data:").split(";")[0] or default_mime
        data = base64.b64decode(b64_data)
    else:
        response = await http_client.get(attachment.url_or_data)
        response.raise_for_status()
        data = response.content
        media_type = attachment.media_type or response.headers.get("content-type", default_mime).split(";")[0]

    return {"inline_data": {"mime_type": media_type, "data": data}}


async def _attachment_parts(attachments: list[Attachment], http_client: httpx.AsyncClient | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    client = http_client
    owns_client = False
    try:
        needs_fetch = any(a.kind != AttachmentKind.AUDIO for a in attachments)
        if client is None and needs_fetch:
            client = httpx.AsyncClient()
            owns_client = True
        for attachment in attachments:
            parts.append(await _attachment_part(attachment, client))  # type: ignore[arg-type]
    finally:
        if owns_client and client is not None:
            await client.aclose()
    return parts


def _tool_declarations(tools: list[dict[str, Any]]) -> list[types.Tool]:
    if not tools:
        return []
    declarations = [
        types.FunctionDeclaration(
            name=t["name"],
            description=t.get("description", ""),
            parameters_json_schema=t.get("input_schema") or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
    return [types.Tool(function_declarations=declarations)]


def _model_parts_from_response(response: Any) -> list[dict[str, Any]]:
    """Rebuilds the model turn's parts from the raw response, preserving each
    part's `thought_signature` — Gemini requires it to be echoed back verbatim
    on any later turn that replays a `function_call` part, and it's lost if we
    reconstruct parts from the `response.text`/`response.function_calls`
    convenience accessors instead of the raw parts."""
    candidate = response.candidates[0] if response.candidates else None
    raw_parts = candidate.content.parts if candidate and candidate.content and candidate.content.parts else []
    parts: list[dict[str, Any]] = []
    for part in raw_parts:
        part_dict: dict[str, Any] = {}
        if part.text:
            part_dict["text"] = part.text
        if part.function_call:
            part_dict["function_call"] = {
                "id": part.function_call.id,
                "name": part.function_call.name,
                "args": part.function_call.args or {},
            }
        if part.thought_signature:
            # base64 text, not raw bytes — this ends up in `conversation.state.history`,
            # persisted as JSON via PostgREST, which can't encode a bytes value.
            part_dict["thought_signature"] = base64.b64encode(part.thought_signature).decode("ascii")
        if part_dict:
            parts.append(part_dict)
    return parts


def _decode_thought_signatures(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reverses `_model_parts_from_response`'s base64 encoding right before a
    request goes out — the API wants the raw bytes back, not the JSON-safe text."""
    decoded = []
    for message in messages:
        parts = []
        for part in message.get("parts", []):
            if isinstance(part, dict) and isinstance(part.get("thought_signature"), str):
                part = {**part, "thought_signature": base64.b64decode(part["thought_signature"])}
            parts.append(part)
        decoded.append({**message, "parts": parts})
    return decoded


def _stringify_tool_result(result: Any) -> dict[str, Any]:
    """Gemini's `function_response.response` wants a dict, not a bare string."""
    if isinstance(result, dict):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    return {"result": result}


async def _run_one_tool(executor: ToolExecutor, call_id: str | None, name: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await executor(name, args)
        response = _stringify_tool_result(result)
    except Exception as exc:  # noqa: BLE001 — any tool failure must come back to the model, not crash the loop
        response = {"error": str(exc)}
    return {"function_response": {"id": call_id, "name": name, "response": response}}


class GeminiEngine:
    def __init__(self, api_key: str, model_name: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model_name = model_name

    async def build_user_message(
        self, text: str | None, attachments: list[Attachment], http_client: httpx.AsyncClient | None = None
    ) -> dict[str, Any]:
        parts = await _attachment_parts(attachments, http_client)
        if text:
            parts.append({"text": text})
        return {"role": "user", "parts": parts or [{"text": ""}]}

    def find_tool_use(self, messages: list[dict[str, Any]], name: str | None = None) -> dict[str, Any] | None:
        if not messages:
            return None
        parts = messages[-1].get("parts")
        if not isinstance(parts, list):
            return None
        for part in parts:
            fc = part.get("function_call") if isinstance(part, dict) else None
            if fc and (name is None or fc.get("name") == name):
                return {"id": fc.get("id"), "name": fc["name"], "input": fc.get("args") or {}}
        return None

    def _config(self, system: str, tools: list[dict[str, Any]], max_tokens: int, web_search: bool) -> types.GenerateContentConfig:
        gemini_tools = _tool_declarations(tools)
        tool_config = None
        if web_search:
            gemini_tools.append(types.Tool(google_search=types.GoogleSearch()))
            if tools:
                # Gemini rejects mixing a server-side tool (google_search) with
                # custom function declarations unless this is set explicitly.
                tool_config = types.ToolConfig(include_server_side_tool_invocations=True)
        return types.GenerateContentConfig(
            system_instruction=system, tools=gemini_tools or None, tool_config=tool_config, max_output_tokens=max_tokens
        )

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
        config = self._config(system, tools, max_tokens, web_search)

        for _ in range(max_iterations):
            response = await self._client.aio.models.generate_content(
                model=self._model_name, contents=_decode_thought_signatures(working), config=config
            )
            function_calls = response.function_calls or []
            text = response.text

            model_parts = _model_parts_from_response(response)
            working = [*working, {"role": "model", "parts": model_parts or [{"text": ""}]}]

            if not function_calls:
                return AgentTurnResult(done=True, final_text=text or "", messages=working)

            if any(fc.name in async_tool_names for fc in function_calls):
                # Don't execute ANY tool in this batch — same reasoning as the
                # Anthropic engine: a side-effecting call must never run only
                # to have its result discarded when the batch needs pausing.
                return AgentTurnResult(done=False, final_text=text or None, messages=working)

            results = await asyncio.gather(*(_run_one_tool(tool_executor, fc.id, fc.name, fc.args or {}) for fc in function_calls))
            working = [*working, {"role": "user", "parts": results}]

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
        pending_parts = [p for p in working[-1]["parts"] if isinstance(p, dict) and p.get("function_call")]

        async def _run(part: dict[str, Any]) -> dict[str, Any]:
            fc = part["function_call"]
            call_id, name, args = fc.get("id"), fc["name"], fc.get("args") or {}
            if call_id in resolved_tool_results:
                response = _stringify_tool_result(resolved_tool_results[call_id])
                return {"function_response": {"id": call_id, "name": name, "response": response}}
            return await _run_one_tool(tool_executor, call_id, name, args)

        results = await asyncio.gather(*(_run(p) for p in pending_parts))
        working = [*working, {"role": "user", "parts": results}]
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
        parts = await _attachment_parts(attachments, None)
        if text:
            parts.append({"text": text})
        schema = model.model_json_schema()
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            response_json_schema=schema,
        )

        contents: list[dict[str, Any]] = [{"role": "user", "parts": parts}]
        last_error: Exception | None = None
        for _ in range(max_retries + 1):
            response = await self._client.aio.models.generate_content(model=self._model_name, contents=contents, config=config)
            try:
                data = json.loads(response.text or "")
                return model.model_validate(data)
            except (ValidationError, json.JSONDecodeError, TypeError) as exc:
                last_error = exc
                contents = [
                    *contents,
                    {"role": "model", "parts": [{"text": response.text or ""}]},
                    {"role": "user", "parts": [{"text": f"That response doesn't match the expected schema ({exc}). Fix it and return valid JSON only."}]},
                ]

        raise RuntimeError(f"Model didn't return a valid '{tool_name}' after {max_retries + 1} attempts: {last_error}")
