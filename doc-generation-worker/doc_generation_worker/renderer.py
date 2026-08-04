"""Turns the model's already-written text into actual file bytes.

Deliberately dumb: the model (via the `generate_document` tool) has already
decided what the document says — this module only encodes it into the
requested container format, no content generation of its own.
"""

from __future__ import annotations

from fpdf import FPDF, XPos, YPos

MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "markdown": "text/markdown",
}

EXTENSIONS: dict[str, str] = {
    "pdf": ".pdf",
    "csv": ".csv",
    "txt": ".txt",
    "markdown": ".md",
}


def _pdf_safe(text: str) -> str:
    """Drops any character the core Helvetica font can't render.

    fpdf2's core (non-embedded) fonts only support `latin-1` — that's fine
    for Spanish text itself (á/é/í/ó/ú/ñ/¿/¡ are all in latin-1), but the
    model routinely writes emoji (SYSTEM_PROMPT explicitly encourages them)
    and "smart" punctuation (em dashes, curly quotes, bullets) that aren't,
    which otherwise crashes `multi_cell` with `FPDFUnicodeEncodingException`
    instead of just rendering without them.
    """
    return text.encode("latin-1", errors="ignore").decode("latin-1")


def _render_pdf(content: str) -> bytes:
    """Minimal text-to-PDF layout: wraps paragraphs, and gives a line a
    bigger/bold font if it looks like a Markdown heading ('# ', '## ', ...) —
    not a real Markdown renderer, just enough to make a generated report
    readable without pulling in a heavier HTML/CSS-based PDF pipeline."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    for raw_line in content.splitlines() or [""]:
        line = _pdf_safe(raw_line)
        stripped = line.lstrip("#").strip()
        heading_level = len(line) - len(line.lstrip("#"))
        # new_x=LMARGIN, new_y=NEXT: multi_cell's own default (new_x=RIGHT)
        # leaves the cursor pinned at the page's right edge after a line that
        # fits on one row — any two consecutive non-blank lines without this
        # then crash with "Not enough horizontal space to render a single
        # character", since the second call starts with zero width left to
        # work with. This was the actual, always-reproducible bug behind that
        # error (not the Unicode content — see _pdf_safe above for that,
        # separate, issue).
        if heading_level and stripped:
            pdf.set_font("Helvetica", style="B", size=max(11, 16 - 2 * heading_level))
            pdf.multi_cell(0, 8, stripped, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", size=11)
        elif line.strip():
            pdf.multi_cell(0, 6, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            pdf.ln(4)

    return bytes(pdf.output())


def render(file_type: str, content: str) -> bytes:
    if file_type == "pdf":
        return _render_pdf(content)
    if file_type in ("csv", "txt", "markdown"):
        text = content if content.endswith("\n") else content + "\n"
        return text.encode("utf-8")
    raise ValueError(f"Unsupported file_type: {file_type!r}")
