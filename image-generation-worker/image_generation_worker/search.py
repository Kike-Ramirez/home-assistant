"""Real image search via Google's Custom Search JSON API (a Programmable
Search Engine configured for image search) — the "find a real photo" half
of the `generate_image` tool, tried before `generate.py`'s AI fallback so a
request for an actual product (e.g. "mi lavadora Balay 3TS984B") gets a real
photo of it when one is findable, not just a plausible-looking approximation.

Optional by design: with no API key/CSE id configured (`image_search_secrets`
in `config.py`), `search_image` returns `None` immediately — the caller
falls straight to Gemini generation, so a household that never sets these up
still gets the feature, just without the "find a real photo first" half.
"""

from __future__ import annotations

import httpx

from .config import image_search_secrets

_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
_ACCEPTED_CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp")
_RESULTS_TO_TRY = 5


async def search_image(query: str) -> bytes | None:
    if not image_search_secrets.api_key or not image_search_secrets.cx:
        return None

    params = {
        "key": image_search_secrets.api_key,
        "cx": image_search_secrets.cx,
        "q": query,
        "searchType": "image",
        "num": _RESULTS_TO_TRY,
        "safe": "active",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(_ENDPOINT, params=params)
        response.raise_for_status()
        items = response.json().get("items", [])

        # Results are just links — the first one that's actually reachable
        # and looks like a real image wins; a dead/blocked link on one result
        # shouldn't sink the whole search when the next one down is fine.
        for item in items:
            link = item.get("link")
            if not link:
                continue
            try:
                image_response = await client.get(link, follow_redirects=True)
                image_response.raise_for_status()
            except httpx.HTTPError:
                continue
            content_type = image_response.headers.get("content-type", "").split(";")[0].strip()
            if content_type in _ACCEPTED_CONTENT_TYPES:
                return image_response.content

    return None
