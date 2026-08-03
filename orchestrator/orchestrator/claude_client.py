"""orchestrator's LLM engine (pluggable — see `shared/shared/engines/`), the
agent loop's system prompt, and reminder wording.

All conversational intelligence lives in `SYSTEM_PROMPT` + the tools in
`orchestrator/tools.py`, driven by `engine.run_agent_loop` from `main.py`.
Nothing here decides intent or branches on it — every "what does the user
want" decision is the model's, made by calling (or not calling) a tool
(CLAUDE.md section 10, "the model drives via tool-use").

Which engine is active is picked here, once, from appconfig — everything
else in the codebase just calls the `Engine` interface and doesn't know or
care whether it's Gemini, Claude, or something added later.

`word_reminder` is the one narrow exception that still calls the model
directly outside the agent loop: it words a *proactive* notification (not a
reply to a user message), so it isn't part of any conversation turn.
"""

from __future__ import annotations

from typing import Any

from shared.engines import get_engine

from .config import SERVICE_NAME, appconfig, system

ENGINE_NAME = appconfig.get("engine", "gemini")
_DEFAULT_MODELS = {"gemini": "gemini-flash-latest", "anthropic": "claude-sonnet-5"}

engine = get_engine(
    ENGINE_NAME,
    SERVICE_NAME,
    appconfig.get("model", _DEFAULT_MODELS.get(ENGINE_NAME, "gemini-flash-latest")),
    system.connect_timeout_seconds,
)


def max_tokens() -> int:
    return appconfig.get("maxTokens", 4096)


_LANGUAGE_INSTRUCTION = "Always reply in the same language the user wrote their message in."

# Spanish, matching every other user-facing reply string in this codebase
# (household's spoken language) — passed into `engine.run_agent_loop`'s
# `max_iterations_fallback`, since `shared.engines` is domain-agnostic infra
# and doesn't hardcode any household-specific wording itself.
MAX_ITERATIONS_FALLBACK = (
    "Se me ha complicado más de la cuenta con esta petición — ¿puedes reformular lo que necesitas "
    "o darme más detalles?"
)

# =============================================================================
# The agent loop's system prompt (used by main.py via engine.run_agent_loop)
# =============================================================================

SYSTEM_PROMPT = (
    "You're a home assistant for a single household, reachable over Telegram and a web chat. "
    "You decide everything yourself by calling whichever tools you need — there's no separate "
    "classification step and no one else deciding what to do with a message. "
    "Use list_devices whenever you need the inventory (troubleshooting, replacement/purchase "
    "recommendations, checking whether a device already exists). "
    "Use create_device / update_device / attach_document to keep the inventory accurate. "
    "Use extract_device_data only when a photo the user sent is genuinely a device label or manual "
    "meant to become a new inventory entry — confirm the extracted details with the user before "
    "calling create_device, and feel free to correct fields yourself if the user points out a mistake "
    "instead of asking a plain yes/no question. "
    "If a photo shows something else — an error code, a screen reading, documentation for a device "
    "that's already registered — do that instead: help troubleshoot it, or call attach_document. "
    "Use your web search tool for anything you don't already know. "
    "Use schedule_reminder whenever the user asks to be reminded about something (maintenance, a "
    "price check, a firmware check), figuring out a sensible scheduled_at and, if they imply "
    "recurrence, an iCal RRULE. "
    "For anything that doesn't need a tool — troubleshooting guidance, a quick course on a topic (with "
    "a short quiz you then grade from the conversation itself), general chat — just answer directly. "
) + _LANGUAGE_INSTRUCTION


# =============================================================================
# Reminders (notifier-scheduler triggers this, orchestrator does the work)
# =============================================================================

REMINDER_SYSTEM_PROMPT = (
    "You're a home assistant writing a short, friendly proactive notification "
    "(one or two sentences) to send to a user about something that's due — a "
    "maintenance reminder, a price drop, or a firmware update. There's no "
    "user message to reply to here (this is a notification, not a reply), so "
    "write it in Spanish — that's this household's language."
)


async def _no_tools(name: str, tool_input: dict[str, Any]) -> Any:
    raise RuntimeError(f"word_reminder has no tools available (unexpected call to {name!r})")


async def word_reminder(kind: str, payload: dict[str, Any]) -> str:
    """Only called when a reminder doesn't already carry a ready-made
    `payload.message` — asks the model to turn the raw kind/payload into a
    proper notification. No tools/search needed for this narrow, free-text
    task, so it's just a one-turn `run_agent_loop` call with an empty tool list."""
    user_message = await engine.build_user_message(f"Reminder kind: {kind}\nDetails: {payload}", [])
    result = await engine.run_agent_loop(
        REMINDER_SYSTEM_PROMPT,
        [],
        _no_tools,
        [user_message],
        max_tokens=max_tokens(),
        web_search=False,
    )
    return result.final_text or ""
