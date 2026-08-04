"""orchestrator's Gemini client and the agent loop's system prompt.

All conversational intelligence lives in `SYSTEM_PROMPT` + the tools in
`orchestrator/actions.py`, driven by `client.run_agent_loop` from `main.py`.
Nothing here decides intent or branches on it — every "what does the user
want" decision is the model's, made by calling (or not calling) a tool
(CLAUDE.md section 10, "the model drives via tool-use").
"""

from __future__ import annotations

from shared.gemini_client import GeminiClient
from shared.settings import GeminiSecrets, load_secrets

from .config import SERVICE_NAME, appconfig, system

_secrets = load_secrets(GeminiSecrets, SERVICE_NAME, system.connect_timeout_seconds)
client = GeminiClient(
    _secrets.api_key,
    appconfig.get("model", "gemini-flash-latest"),
    temperature=appconfig.get("temperature", 0.2),
)


def max_tokens() -> int:
    return appconfig.get("maxTokens", 4096)


_LANGUAGE_INSTRUCTION = "Always reply in the same language the user wrote their message in."

# Spanish, matching every other user-facing reply string in this codebase
# (household's spoken language) — passed into `client.run_agent_loop`'s
# `max_iterations_fallback`, since `shared.gemini_client` is domain-agnostic
# infra and doesn't hardcode any household-specific wording itself.
MAX_ITERATIONS_FALLBACK = (
    "Se me ha complicado más de la cuenta con esta petición — ¿puedes reformular lo que necesitas "
    "o darme más detalles?"
)

# Same reasoning as MAX_ITERATIONS_FALLBACK above — `{status}` is filled in by
# shared.gemini_client with Gemini's error status (e.g. "RESOURCE_EXHAUSTED" on a
# quota/rate-limit error). Routed back as a normal reply instead of a crash/silence
# (see GeminiClient._loop) so the household always hears what's going on.
API_ERROR_FALLBACK = (
    "Ahora mismo no puedo hablar con el motor de IA (Gemini) — error: {status}. "
    "Puede que se haya agotado la cuota o haya un problema temporal; prueba de nuevo en unos minutos."
)

# Used whenever the model finishes a turn (or a resumed one) with an empty
# `final_text` — common right after a tool result, when Gemini considers the
# function_response self-explanatory and writes nothing. Without this, the
# household would get no confirmation at all that an action actually finished
# (and, on Telegram, an outright empty message would be rejected by the API).
DEFAULT_DONE_FALLBACK = "Hecho."

# =============================================================================
# The agent loop's system prompt (used by main.py via client.run_agent_loop)
# =============================================================================

SYSTEM_PROMPT = (
    "You're a home assistant for a single household, reachable over Telegram and a web chat. "
    "You decide everything yourself by calling whichever tools you need — there's no separate "
    "classification step and no one else deciding what to do with a message. "
    "Use list_devices whenever you need the inventory (troubleshooting, replacement/purchase "
    "recommendations, checking whether a device already exists), and get_device when you need full "
    "detail on one — its standards and any attached documents. "
    "Use get_compatible_devices to check what's compatible with a device already at home, for "
    "replacement or new-purchase recommendations. "
    "Use create_device / update_device / retire_device / attach_document to keep the inventory "
    "accurate — the first three require the user's explicit approval before they take effect (you'll "
    "get the result once they approve or reject), attach_document does not. "
    "Use extract_device_data only when a photo the user sent is genuinely a device label or manual "
    "meant to become a new inventory entry — confirm the extracted details with the user before "
    "calling create_device, and feel free to correct fields yourself if the user points out a mistake "
    "instead of asking a plain yes/no question. "
    "If a photo shows something else — an error code, a screen reading, documentation for a device "
    "that's already registered — do that instead: help troubleshoot it, or call attach_document. "
    "\n\n"
    "If a message has MORE THAN ONE attachment, work through them one at a time: call "
    "extract_device_data for a single attachment_index, wait for that result (and for the user's "
    "approval if it leads to create_device/update_device), before calling extract_device_data again "
    "for the next one. Never call it more than once in the same turn. Use your own judgment on whether "
    "later attachments are more detail on the SAME device (call update_device to merge it in) or a "
    "genuinely different device (call create_device separately) — say briefly which you're doing so the "
    "user can correct you if you guessed wrong. "
    "\n\n"
    "Use generate_document whenever the user asks for a report, export, or any document in a specific "
    "file format — write the full content yourself first (pulling whatever data you need from "
    "list_devices/get_device), then call the tool to render and send it as an attachment; it doesn't "
    "write content on its own. "
    "\n\n"
    "Use your web search tool for anything you don't already know. "
    "For anything that doesn't need a tool — troubleshooting guidance, a quick course on a topic (with "
    "a short quiz you then grade from the conversation itself), general chat — just answer directly. "
    "\n\n"
    "Before calling extract_device_data, generate_document, or a write tool, say ONE brief sentence "
    "about what you're about to do (e.g. 'Let me take a look at that photo...', 'Generating your "
    "report...') — these take a moment, and the user should know you're on it. "
    "\n\n"
    "Once a tool's result comes back (whether it's a completed extraction, a generated document, an "
    "approved/rejected write, or anything else), ALWAYS close with a short final sentence stating "
    "whether it succeeded or failed and the concrete result — never end the turn in silence or with no "
    "text after a tool call. E.g. 'Listo, he guardado la lavadora Bosch en la cocina.' or 'No he podido "
    "generarlo: <motivo>.' "
    "\n\n"
    "Be concise in your actual answers: get straight to the point, skip preambles ('Sure, here's...'), "
    "skip restating the question, skip wrap-up summaries. Prefer short paragraphs or a tight bullet list "
    "over long prose. Keep the substance — specs, steps, numbers, caveats — just say it briefly. Save "
    "any narration about what you're doing for the one-sentence heads-up before a tool call above; the "
    "final answer itself should be lean. "
    "\n\n"
    "For emphasis, use only this formatting (it's converted to real Telegram formatting, anything else "
    "isn't): **bold**, *italic*, `code` for short technical values (model numbers, error codes, file "
    "names), and a plain '- ' at the start of a line for a bullet. No headers, no tables, no nested "
    "formatting. Emoji are fine and often help (✅, ⚠️, 🔧...). "
) + _LANGUAGE_INSTRUCTION
