"""Converts the model's lightweight Markdown subset into Telegram-safe HTML.

`bot` is configured with `parse_mode=HTML` (see main.py) — HTML needs far
less escaping than Telegram's own MarkdownV2 (only `&`/`<`/`>`, versus almost
every punctuation character), so it's the safer target to convert into.

Only a small, unambiguous subset is supported, matching what `SYSTEM_PROMPT`
(orchestrator/orchestrator/llm.py) asks the model to stick to: `**bold**`,
`*italic*`, and `` `code` ``. Bullet points are plain "- " text, left alone —
Telegram has no list element, and a plain dash reads fine either way.
Anything else (headers, tables, nested/mixed emphasis) isn't converted; if
the model ever writes it anyway, it just shows up as literal characters
instead of breaking the message.
"""

from __future__ import annotations

import html
import re

_CODE = re.compile(r"`([^`\n]+?)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"\*(.+?)\*", re.DOTALL)
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


def markdown_to_telegram_html(text: str) -> str:
    escaped = html.escape(text, quote=False)

    code_spans: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        code_spans.append(match.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    # Stashed before bold/italic so `snake_case_names` or `a*b` inside a code
    # span never get misread as emphasis markers.
    escaped = _CODE.sub(_stash_code, escaped)
    escaped = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    return _PLACEHOLDER.sub(lambda m: f"<code>{code_spans[int(m.group(1))]}</code>", escaped)
