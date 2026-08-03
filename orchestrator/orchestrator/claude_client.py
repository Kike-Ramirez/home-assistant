"""orchestrator's Claude client, the agent loop's system prompt, and reminder wording.

All conversational intelligence lives in `SYSTEM_PROMPT` + the tools in
`orchestrator/tools.py`, driven by `shared.claude.run_agent_loop` from
`main.py`. Nothing here decides intent or branches on it — every "what does
the user want" decision is Claude's, made by calling (or not calling) a tool
(CLAUDE.md section 10, "Claude drives via tool-use").

`word_reminder` is the one narrow exception that still calls Claude directly
outside the agent loop: it words a *proactive* notification (not a reply to
a user message), so it isn't part of any conversation turn.
"""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic
from shared.claude import extract_text

from .config import anthropic_secrets, appconfig

client = AsyncAnthropic(api_key=anthropic_secrets.api_key)


def claude_model() -> str:
    return appconfig.get("claudeModel", "claude-sonnet-5")


def max_tokens() -> int:
    return appconfig.get("maxTokens", 4096)


_LANGUAGE_INSTRUCTION = "Always reply in the same language the user wrote their message in."

# Spanish, matching every other user-facing reply string in this codebase
# (household's spoken language) — passed into shared.claude.run_agent_loop's
# `max_iterations_fallback`, since that module is domain-agnostic infra and
# doesn't hardcode any household-specific wording itself.
MAX_ITERATIONS_FALLBACK = (
    "Se me ha complicado más de la cuenta con esta petición — ¿puedes reformular lo que necesitas "
    "o darme más detalles?"
)

# =============================================================================
# The agent loop's system prompt (used by main.py via shared.claude.run_agent_loop)
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
    "Use web_search for anything you don't already know, and web_fetch for a URL the user shared. "
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


async def word_reminder(kind: str, payload: dict[str, Any]) -> str:
    """Only called when a reminder doesn't already carry a ready-made
    `payload.message` — asks Claude to turn the raw kind/payload into a
    proper notification. No tools needed for this narrow, free-text task."""
    response = await client.messages.create(
        model=claude_model(),
        max_tokens=max_tokens(),
        system=REMINDER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Reminder kind: {kind}\nDetails: {payload}"}],
    )
    return extract_text(response)
