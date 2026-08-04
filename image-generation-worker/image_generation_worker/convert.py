"""Normalizes whatever image bytes came back — a search result or Gemini's
own generated output, either of which can be PNG/WEBP/etc. — into a real
JPEG, per the `generate_image` tool's contract (CLAUDE.md: images are always
delivered as `.jpg`, whatever format they started as)."""

from __future__ import annotations

import io

from PIL import Image

JPEG_MEDIA_TYPE = "image/jpeg"


def to_jpeg(data: bytes) -> bytes:
    image = Image.open(io.BytesIO(data))
    if image.mode not in ("RGB", "L"):
        # JPEG has no alpha channel — flatten anything with one (PNG/WEBP
        # with transparency) onto plain RGB instead of letting save() fail.
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()
