"""Turns the model's already-written text into actual file bytes.

Deliberately dumb: the model (via the `generate_document` tool) has already
decided what the document says — this module only encodes it into the
requested container format, no content generation of its own.
"""

from __future__ import annotations

from fpdf import FPDF

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


def _render_pdf(content: str) -> bytes:
    """Minimal text-to-PDF layout: wraps paragraphs, and gives a line a
    bigger/bold font if it looks like a Markdown heading ('# ', '## ', ...) —
    not a real Markdown renderer, just enough to make a generated report
    readable without pulling in a heavier HTML/CSS-based PDF pipeline."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    for line in content.splitlines() or [""]:
        stripped = line.lstrip("#").strip()
        heading_level = len(line) - len(line.lstrip("#"))
        if heading_level and stripped:
            pdf.set_font("Helvetica", style="B", size=max(11, 16 - 2 * heading_level))
            pdf.multi_cell(0, 8, stripped)
            pdf.set_font("Helvetica", size=11)
        elif line.strip():
            pdf.multi_cell(0, 6, line)
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
