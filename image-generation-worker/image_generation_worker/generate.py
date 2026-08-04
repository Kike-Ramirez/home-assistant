"""AI-generated fallback for the `generate_image` tool, used when
`search.py::search_image` finds nothing usable (or isn't configured at all).
A single, direct Gemini image-generation call — no agent loop, no tool-use;
this service only ever produces one image per request.

Model name is appconfig-driven (`imageModel`), not hardcoded, on purpose:
CLAUDE.md's own history has an exact precedent for this class of breakage
(`gemini-2.5-flash` 404ing despite still listing in `client.models.list()`)
— the default below hasn't been verified against the live API the same way
`gemini-flash-latest` was for the text model, so treat it as a starting
point to confirm/adjust, not a guarantee.
"""

from __future__ import annotations

from google import genai
from google.genai import types

from .config import appconfig, gemini_secrets

_client = genai.Client(api_key=gemini_secrets.api_key)


async def generate_image(prompt: str) -> bytes:
    model = appconfig.get("imageModel", "gemini-2.5-flash-image")
    response = await _client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    candidate = response.candidates[0] if response.candidates else None
    parts = candidate.content.parts if candidate and candidate.content and candidate.content.parts else []
    for part in parts:
        if part.inline_data is not None and part.inline_data.data:
            return part.inline_data.data

    raise RuntimeError(f"Gemini model {model!r} didn't return any image data for this prompt")
