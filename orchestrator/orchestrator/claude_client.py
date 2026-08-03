"""All of the orchestrator's conversational intelligence lives here.

Explicit design principle: any interpretation of what the user said (what
they want, whether they confirmed something, which quiz option they picked)
is delegated to Claude with structured output (`shared.claude.call_structured`)
instead of keyword heuristics — much more robust against natural language,
typos, or different languages.

NOTE: the exact name/version of the built-in web search tool
(`web_search_20250305` as of this session) may change — check it against the
current `anthropic` SDK docs before deploying.
"""

from __future__ import annotations

from typing import Any, Literal

from anthropic import Anthropic
from pydantic import BaseModel
from shared.claude import call_structured, extract_text

from .config import anthropic_secrets, appconfig

_client = Anthropic(api_key=anthropic_secrets.api_key)

_WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

_LANGUAGE_INSTRUCTION = "Always reply in the same language the user wrote their message in."


def _claude_model() -> str:
    return appconfig.get("claudeModel", "claude-sonnet-5")


def _web_search_tools() -> list[dict[str, Any]]:
    return [_WEB_SEARCH_TOOL] if appconfig.get("webSearchEnabled", True) else []


def _ask(system_prompt: str, user_content: str) -> str:
    """Free-text (non-structured) question, with `web_search` available."""
    response = _client.messages.create(
        model=_claude_model(),
        max_tokens=appconfig.get("maxTokens", 4096),
        system=system_prompt,
        tools=_web_search_tools(),
        messages=[{"role": "user", "content": user_content}],
    )
    return extract_text(response)


def _format_devices(devices: list[dict[str, Any]], *, include_standards: bool = False) -> str:
    if not devices:
        return "No devices registered in the house yet."
    lines = []
    for d in devices:
        base = f"- {d['display_name']} (brand: {d.get('brand')}, model: {d.get('model')}, location: {d.get('isa95_area')}"
        if include_standards:
            standards = ", ".join(
                entry["standard"]["name"] for entry in d.get("device_standard", []) if entry.get("standard")
            )
            lines.append(f"{base}, standards: {standards or 'unknown'})")
        else:
            lines.append(f"{base}, attributes: {d.get('attributes')})")
    return "\n".join(lines)


# =============================================================================
# Flow 2: troubleshooting
# =============================================================================

TROUBLESHOOTING_SYSTEM_PROMPT = (
    "You're a home assistant helping the user troubleshoot appliances and "
    "devices around the house. You're given the full inventory of devices in "
    "the home; figure out yourself which one (if any) the question is about. "
    "If you have its spec sheet, lean on that first. If you need more info "
    "(manuals, forums, known fixes), use web search. Always answer with a "
    "clear, step-by-step guide. "
) + _LANGUAGE_INSTRUCTION


def ask_troubleshooting(devices: list[dict[str, Any]], question: str) -> str:
    inventory = _format_devices(devices)
    return _ask(TROUBLESHOOTING_SYSTEM_PROMPT, f"Home inventory:\n{inventory}\n\nQuestion: {question}")


# =============================================================================
# Intent classification (replaces any keyword-matching heuristic)
# =============================================================================


class IntentClassification(BaseModel):
    intent: Literal["troubleshooting", "course", "replacement", "other"]
    topic: str | None = None


def classify_intent(user_text: str) -> IntentClassification:
    return call_structured(
        _client,
        _claude_model(),
        system=(
            "Classify the intent of this message from a user talking to a home "
            "assistant (device onboarding is handled separately; classify only "
            "between troubleshooting, quick course, replacement/purchase, or "
            "general chit-chat). `topic`: the course topic, or the device/"
            "category for a replacement request; null if not applicable."
        ),
        user_content=user_text,
        tool_name="classify_intent",
        model=IntentClassification,
    )


# =============================================================================
# Confirmation interpretation (flow 1: "is this correct? yes/no")
# =============================================================================


class ConfirmationResult(BaseModel):
    confirmed: bool | None  # None if the reply is ambiguous


def interpret_confirmation(user_reply: str) -> bool | None:
    result = call_structured(
        _client,
        _claude_model(),
        system="Determine whether the user is confirming or rejecting a proposal, in whatever language or phrasing they used.",
        user_content=user_reply,
        tool_name="interpret_confirmation",
        model=ConfirmationResult,
    )
    return result.confirmed


# =============================================================================
# Flow 3: quick course + quiz
# =============================================================================


class QuizQuestion(BaseModel):
    question: str
    options: list[str]
    correct_index: int


class CourseResult(BaseModel):
    lesson: str
    questions: list[QuizQuestion]


def generate_course(topic: str) -> CourseResult:
    return call_structured(
        _client,
        _claude_model(),
        system=(
            "You're a teacher who writes short, clear crash courses on any "
            "topic for a general audience, followed by a multiple-choice quiz "
            "(3 to 5 questions, 2 to 4 options each) to check what stuck. "
        )
        + _LANGUAGE_INSTRUCTION,
        user_content=f"Write a quick course with a quiz about: {topic}",
        tool_name="generate_course",
        model=CourseResult,
        max_tokens=2048,
    )


class QuizAnswerResult(BaseModel):
    selected_index: int | None  # None if it's unclear which option is meant


def interpret_quiz_answer(question: str, options: list[str], user_reply: str) -> int | None:
    options_text = "\n".join(f"{i}: {opt}" for i, opt in enumerate(options))
    result = call_structured(
        _client,
        _claude_model(),
        system=(
            "The user is answering a multiple-choice quiz question, in free-form "
            "language (they might say the letter, the number, the option's text, "
            "or paraphrase it). Figure out which option they mean."
        ),
        user_content=f"Question: {question}\nOptions:\n{options_text}\n\nUser's reply: {user_reply}",
        tool_name="interpret_answer",
        model=QuizAnswerResult,
    )
    return result.selected_index


# =============================================================================
# Flow 4: replacement / new purchase
# =============================================================================

REPLACEMENT_SYSTEM_PROMPT = (
    "You're a home assistant recommending new or replacement devices, "
    "prioritizing compatibility with what's already in the house (same "
    "standard/protocol, or known compatibility). Use web search to check "
    "current popularity and pricing. Return a ranked list of options with "
    "sources cited. "
) + _LANGUAGE_INSTRUCTION


def ask_replacement(existing_devices: list[dict[str, Any]], request_text: str) -> str:
    inventory = _format_devices(existing_devices, include_standards=True)
    return _ask(REPLACEMENT_SYSTEM_PROMPT, f"Devices currently in the house:\n{inventory}\n\nUser's request: {request_text}")
