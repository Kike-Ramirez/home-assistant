"""The one boundary between orchestrator's business logic (`main.py`,
`tools.py`) and whichever LLM API is actually driving the agent loop.
Swapping providers means swapping the `Engine` implementation via
`get_engine()` (see `__init__.py`) — nothing else in the codebase should ever
import `anthropic`/`google.genai` directly outside this package.

Every method takes/returns provider-agnostic shapes EXCEPT `messages`
(`AgentTurnResult.messages`, and the `messages` argument to
`run_agent_loop`/`resume_agent_loop`): that's each provider's own native
wire format, opaque to callers except through `find_tool_use()` and
`build_user_message()`. Conversation history therefore isn't portable across
engines — acceptable, since a deployment picks one engine and stays on it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    import httpx

    from ..message import Attachment

ModelT = TypeVar("ModelT", bound=BaseModel)
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]

DEFAULT_MAX_ITERATIONS_FALLBACK = "I got stuck in a loop on this one — could you rephrase what you need, or give me more detail?"


@dataclass
class AgentTurnResult:
    """Outcome of one `run_agent_loop`/`resume_agent_loop` call.

    When `done` is False, `messages` ends with the turn that requested an
    async tool (one whose name is in `async_tool_names`) — the caller is
    expected to persist `messages` verbatim and later call
    `resume_agent_loop` with that same list plus the resolved result.
    `final_text` in that case is whatever text the model wrote before making
    the tool call (often present), used as a provisional reply; it can be empty.
    """

    done: bool
    final_text: str | None
    messages: list[dict[str, Any]]


class Engine(Protocol):
    """Implemented by `anthropic_engine.AnthropicEngine` and
    `gemini_engine.GeminiEngine`. Each owns its own API client, constructed
    from secrets in `get_engine()`."""

    async def build_user_message(
        self, text: str | None, attachments: list["Attachment"], http_client: "httpx.AsyncClient | None" = None
    ) -> dict[str, Any]:
        """Builds one user turn (in this engine's native message shape) from
        text + attachments, ready to append to a `messages` list."""
        ...

    def find_tool_use(self, messages: list[dict[str, Any]], name: str | None = None) -> dict[str, Any] | None:
        """Finds a pending tool call in the last message, normalized to
        `{"id": ..., "name": ..., "input": {...}}` regardless of provider —
        used by callers that need to know which tool call is pending (e.g.
        to fire real async work, or to build `resolved_tool_results`)."""
        ...

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
        """Runs the model to completion (or `max_iterations`), executing
        whatever tools it calls along the way. `tools` is a list of
        `{"name", "description", "input_schema"}` dicts (JSON Schema,
        provider-agnostic) — `tools.py`'s `TOOL_SCHEMAS`, unchanged across
        engines. `web_search` toggles this engine's own built-in
        search/fetch tool(s), if it has any."""
        ...

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
        """Continues a turn `run_agent_loop` paused. `messages` must be
        exactly what that call returned. `resolved_tool_results` maps
        tool-call id (each unique per call) to the now-known result for
        whichever tool(s) triggered the pause; any other tool in that same
        batch is executed for real now, same as usual."""
        ...

    async def call_structured(
        self,
        system: str,
        text: str | None,
        attachments: list["Attachment"],
        tool_name: str,
        model: type[ModelT],
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> ModelT:
        """Forces the model to return an object validated against `model`
        (retrying up to `max_retries` times on a schema mismatch) —
        `doc-ingestion-worker`'s vision extraction. `attachments` lets the
        caller pass an image/document alongside `text` without needing to
        know this engine's own content-block format."""
        ...
