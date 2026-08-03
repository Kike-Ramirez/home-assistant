"""Human-in-the-Loop approval gate for critical (write) tool calls.

`actions.py` is the plain function dispatcher — it executes whatever it's
told, no judgment involved. This module is the one place that judgment gets
applied: before a call to a tool in `actions.CONFIRM_TOOL_NAMES`
(`create_device`, `update_device`, `retire_device`) actually reaches
PostgREST, a human has to approve it via the channel it came in on.

Only one action can be pending per conversation at a time — same constraint
`extract_device_data`'s async pause already has (`orchestrator/main.py`'s
`pending_agent_turn`) — so resolving a decision needs no correlation id: the
button's `value` ("approve"/"reject") or a parsed sí/no text reply is enough
to know what to do with whatever's in `conversation.state.pending_confirmation`.
"""

from __future__ import annotations

from typing import Any

from shared.message import Action
from shared.postgrest_client import PostgrestClient

from . import actions

APPROVE_ACTIONS = [Action(label="✅ Aprobar", value="approve"), Action(label="❌ Rechazar", value="reject")]

_AFFIRMATIVE_REPLIES = {"approve", "aprobar", "apruebo", "sí", "si", "yes", "ok", "vale"}
_NEGATIVE_REPLIES = {"reject", "rechazar", "rechazo", "no"}

REJECTED_RESULT = {"success": False, "error": "El usuario ha rechazado esta acción."}


def parse_confirmation_text(content: str | None) -> bool | None:
    """Best-effort yes/no parse for channels without real buttons (web-adapter)
    or a Telegram user who typed instead of tapping a button — `None` means
    "not a recognizable decision", so the caller keeps waiting instead of
    guessing what the user meant."""
    normalized = (content or "").strip().lower()
    if normalized in _AFFIRMATIVE_REPLIES:
        return True
    if normalized in _NEGATIVE_REPLIES:
        return False
    return None


def confirmation_prompt(tool_name: str, model_text: str | None) -> str:
    """The model is prompted (see `llm.SYSTEM_PROMPT`) to explain a write
    action in its own words before calling the tool — `model_text` is that
    explanation, used as the approval prompt. Falls back to a generic prompt
    if the model didn't say anything (it usually does)."""
    return model_text or f"¿Confirmas esta acción? ({tool_name})"


async def resolve(pg: PostgrestClient, pending: dict[str, Any], approved: bool) -> Any:
    """The one place a `CONFIRM_TOOL_NAMES` call actually reaches PostgREST —
    everywhere else it's just a paused tool_use waiting on this decision.
    Rejection isn't an error: it's a normal tool result the model reacts to,
    same shape as a failed dispatch."""
    if not approved:
        return REJECTED_RESULT
    try:
        return await actions.dispatch(pg, pending["tool_name"], pending["tool_input"])
    except Exception as exc:  # noqa: BLE001 — any dispatch failure must come back to the model, not crash
        return {"success": False, "error": str(exc)}
